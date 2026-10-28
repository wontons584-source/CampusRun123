#!/usr/bin/env python
"""端到端演示脚本：注册 → 发单 → 抢单 → 履约 → 确认收货。

## 这个脚本存在的意义

单测与集成测试各自只覆盖一段；**没有任何一个测试**能回答"整条链路现在是不是
真的能跑通"。评审/录屏时需要的正是这一点：一次调用、每一步都打印 HTTP 状态码
与业务码，走的是真实的 FastAPI 栈（`TestClient` = 真实 ASGI，不 mock），
背后是真实的 PG + Redis。

## 跑法

```bash
export DATABASE_URL="postgresql+psycopg://campusrun:campusrun@127.0.0.1:5433/campusrun"
export REDIS_URL="redis://127.0.0.1:6380/0"
python scripts/e2e_demo.py
```

退出码 0 = 全链路通过；非 0 = 有一步不符合预期（脚本会明确指出是哪一步）。

## 两个容易被当成"脚本写错了"的现象

1. **发单时间窗**（BR-10：07:00–23:00，校园时区）。半夜跑这个脚本时发单会
   返回 `400/40009`，那不是 bug 而是业务规则。脚本在这种情况下会把**进程时钟**
   冻结到当天 10:00 再跑（只影响本进程，不改库、不改配置），并在开头明确打印。
2. **手机号唯一**。脚本每次生成随机手机号，所以可以反复跑；同一秒跑两次
   有可能撞号（概率 ~1e-8），撞了会看到 `409/40900`。
"""

from __future__ import annotations

import datetime
import os
import random
import sys
from pathlib import Path
from typing import Any, Final

#: `backend/` 根目录：脚本能从任意 cwd 调用
BACKEND_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
for _path in (BACKEND_ROOT, BACKEND_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# ⚠️ 环境变量必须在 import app.* **之前**设置：`app.core.config.settings` 在
# import 期就实例化了，之后再改环境变量不会生效（表现是"连到了默认的 5432"）。
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://campusrun:campusrun@127.0.0.1:5433/campusrun"
)
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6380/0")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.clock import UTC, clock as process_clock  # noqa: E402
from app.db.session import engine  # noqa: E402
from app.main import app  # noqa: E402
from seed_pois import load_rows, seed_pois  # noqa: E402

#: 发单窗（BR-10），与 `Thresholds.send_window_*` 的默认值一致
SEND_WINDOW_START: Final[int] = 7
SEND_WINDOW_END: Final[int] = 23

#: 冻结时钟用的"安全时刻"：UTC 02:00 = 北京时间 10:00（窗内但不是窗边）
_FROZEN_AT: Final[datetime.datetime] = datetime.datetime(2026, 5, 20, 2, 0, tzinfo=UTC)

#: 脚本内的断言计数
_checks: list[tuple[str, bool, str]] = []


def hr(title: str) -> None:
    """分节标题。"""
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def check(label: str, ok: bool, detail: str = "") -> None:
    """记录一条断言（不中断，最后统一汇总）。"""
    _checks.append((label, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def random_phone(prefix: str = "139") -> str:
    """生成一个合法的 11 位手机号（`1[3-9]` 开头，避免与种子数据撞号）。"""
    return f"{prefix}{random.randint(0, 9999_9999):08d}"


class Api:
    """极薄的 HTTP 调用包装：打印状态码 + 业务码，返回解析后的 JSON。"""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: dict[str, Any] | None = None,
        note: str = "",
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self._client.request(method, path, json=body, headers=headers)
        try:
            payload: dict[str, Any] = response.json()
        except ValueError:  # pragma: no cover - 正常不会发生
            payload = {"raw": response.text}
        suffix = f"  {note}" if note else ""
        print(
            f"  {method:<5} {path:<38} → HTTP {response.status_code:<3} "
            f"code={payload.get('code')!s:<6}{suffix}"
        )
        return response.status_code, payload


def _ensure_send_window() -> None:
    """若当前不在发单窗内，冻结进程时钟到窗内（只影响本进程）。"""
    now_local = process_clock.to_local(process_clock.now())
    if SEND_WINDOW_START <= now_local.hour < SEND_WINDOW_END:
        print(f"[clock] 当前 {now_local:%Y-%m-%d %H:%M} 在发单窗内，使用真实时钟")
        return
    process_clock.freeze(_FROZEN_AT)
    frozen_local = process_clock.to_local(process_clock.now())
    print(
        f"[clock] 当前 {now_local:%H:%M} 不在发单窗（07:00–23:00），"
        f"进程时钟已冻结到 {frozen_local:%Y-%m-%d %H:%M}（不动库、不改配置）"
    )


def _ensure_pois() -> int:
    """灌 POI 种子（幂等），返回 `pois` 总数。"""
    with engine.begin() as connection:
        inserted = seed_pois(connection, load_rows())
        total = int(connection.exec_driver_sql("SELECT count(*) FROM pois").scalar_one())
    print(f"[seed] POI 种子就绪：本次新增 {inserted} 条，库内共 {total} 条")
    return total


def main() -> int:
    """跑完整条链路，返回进程退出码。"""
    hr("0. 环境准备")
    print(f"[env] DATABASE_URL={os.environ['DATABASE_URL']}")
    print(f"[env] REDIS_URL={os.environ['REDIS_URL']}")
    _ensure_send_window()
    if _ensure_pois() == 0:
        print("[fatal] POI 表为空，无法发单（BR-08）", file=sys.stderr)
        return 2

    with TestClient(app, raise_server_exceptions=False) as client:
        api = Api(client)

        # ------------------------------------------------------------ 认证
        hr("1. 注册两个账号（发单人 A / 接单人 B）")
        a_phone, b_phone = random_phone("139"), random_phone("138")
        password = "Passw0rd!"
        status, a_reg = api.call(
            "POST",
            "/api/v1/auth/register",
            body={"nickname": "发单人A", "phone": a_phone, "password": password},
            note="发单人 A",
        )
        check("A 注册返回 201", status == 201, f"HTTP {status}")
        a_token = (a_reg.get("data") or {}).get("token", "")
        a_id = ((a_reg.get("data") or {}).get("user") or {}).get("id")
        check("A 拿到 token", bool(a_token))

        status, b_reg = api.call(
            "POST",
            "/api/v1/auth/register",
            body={"nickname": "接单人B", "phone": b_phone, "password": password},
            note="接单人 B",
        )
        check("B 注册返回 201", status == 201, f"HTTP {status}")
        b_token = (b_reg.get("data") or {}).get("token", "")
        b_id = ((b_reg.get("data") or {}).get("user") or {}).get("id")

        status, _ = api.call("POST", "/api/v1/auth/login", body={"phone": a_phone, "password": password}, note="A 登录")
        check("A 登录成功", status == 200, f"HTTP {status}")
        status, _ = api.call(
            "POST", "/api/v1/auth/login", body={"phone": a_phone, "password": "wrong-password"}, note="错误密码"
        )
        check("错误密码返回 401", status == 401, f"HTTP {status}")

        status, me = api.call("GET", "/api/v1/me", token=a_token, note="A 的当前用户")
        check("GET /me 返回 A", status == 200 and (me.get("data") or {}).get("id") == a_id)
        check(
            "GET /me 不含密文手机号",
            "phone_enc" not in str(me) and "password_hash" not in str(me),
        )

        # -------------------------------------------------------------- POI
        hr("2. POI 字典")
        status, pois = api.call("GET", "/api/v1/pois")
        items = pois.get("data") or []
        check("POI 列表非空", status == 200 and len(items) > 0, f"{len(items)} 条")
        station = next((p for p in items if p["type"] == "STATION"), None)
        dorm = next((p for p in items if p["type"] == "DORM"), None)
        if station is None or dorm is None:  # pragma: no cover - 种子缺失
            print("[fatal] 缺少 STATION 或 DORM 类型的 POI", file=sys.stderr)
            return 2
        print(f"      起点：{station['name']}  终点：{dorm['name']}")

        # ------------------------------------------------------------- 发单
        hr("3. A 发单（T1：PENDING）")
        status, created = api.call(
            "POST",
            "/api/v1/orders",
            token=a_token,
            body={
                "type": "EXPRESS",
                "from_poi_id": station["id"],
                "to_poi_id": dorm["id"],
                "reward": "5.5",
                "expect_minutes": 60,
                "description": "顺丰一个中件，麻烦送梅苑1号楼门口",
                "pick_code": "8-2-5031",
            },
        )
        check("发单返回 201", status == 201, f"HTTP {status}")
        order_id = (created.get("data") or {}).get("id")
        check("发单返回 order_id", isinstance(order_id, int), f"id={order_id}")
        check("发单初始状态 PENDING", (created.get("data") or {}).get("status") == "PENDING")

        # ------------------------------------------------------------- 大厅
        hr("4. B 看大厅（T-20 前置：只应看到 PENDING 且不含取件码）")
        status, hall = api.call("GET", "/api/v1/orders/hall", token=b_token)
        hall_items = (hall.get("data") or {}).get("items") or []
        mine = [item for item in hall_items if item["id"] == order_id]
        check("大厅能查到该单", status == 200 and len(mine) == 1, f"大厅 {len(hall_items)} 条")
        check("大厅响应不含 pick_code", "pick_code" not in str(hall))
        if mine:
            check("大厅条目带 from/to 名称与 available_actions",
                  bool(mine[0].get("from_poi_name")) and "ACCEPT" in (mine[0].get("available_actions") or []))
            check("大厅条目带 reward / deadline", mine[0].get("reward") == "5.50" and bool(mine[0].get("deadline")))

        # ------------------------------------------------------------- 抢单
        hr("5. 抢单（T2：PENDING → IN_PROGRESS）")
        status, own = api.call(
            "POST", f"/api/v1/orders/{order_id}/grab", token=a_token, note="A 抢自己的单 → 应 40303"
        )
        check("不能接自己的单（403/40303）", status == 403 and own.get("code") == 40303, f"code={own.get('code')}")

        status, grabbed = api.call(
            "POST", f"/api/v1/orders/{order_id}/grab", token=b_token, note="B 抢单"
        )
        check("B 抢单成功", status == 200 and (grabbed.get("data") or {}).get("status") == "IN_PROGRESS")
        check("抢单回到 T2 且写入 runner_id",
              (grabbed.get("data") or {}).get("transition") == "T2"
              and (grabbed.get("data") or {}).get("runner_id") == b_id)

        status, again = api.call(
            "POST", f"/api/v1/orders/{order_id}/grab", token=b_token, note="重复抢 → 应 409"
        )
        check("重复抢单被拒（409）", status == 409, f"code={again.get('code')}")

        # ------------------------------------------------------------- 详情
        hr("6. 详情（取件码可见性：当事人明文 / 他人不可见）")
        status, detail_a = api.call("GET", f"/api/v1/orders/{order_id}", token=a_token, note="发单人视角")
        check("发单人能看到明文取件码",
              status == 200 and (detail_a.get("data") or {}).get("pick_code") == "8-2-5031",
              f"pick_code={(detail_a.get('data') or {}).get('pick_code')}")
        status, detail_b = api.call("GET", f"/api/v1/orders/{order_id}", token=b_token, note="接单人视角")
        check("已抢单的接单人也能看到明文取件码",
              status == 200 and (detail_b.get("data") or {}).get("pick_code") == "8-2-5031")
        status, detail_x = api.call("GET", f"/api/v1/orders/{order_id}", token=None, note="未登录 → 401")
        check("未登录不能看详情（401）", status == 401, f"HTTP {status}")

        # ------------------------------------------------------------- 履约
        hr("7. 履约进度（T5/T7：已取件 → 配送中 → 已送达）")
        status, jumped = api.call(
            "POST",
            f"/api/v1/orders/{order_id}/progress",
            token=b_token,
            body={"action": "DELIVER"},
            note="跳级送达 → 应 42201",
        )
        check("跳级推进被状态机拒绝（422/42201）",
              status == 422 and jumped.get("code") == 42201, f"code={jumped.get('code')}")

        for action, expect_status in (("PICKED_UP", "IN_PROGRESS"), ("ON_THE_WAY", "IN_PROGRESS"), ("DELIVER", "DELIVERED")):
            status, step = api.call(
                "POST",
                f"/api/v1/orders/{order_id}/progress",
                token=b_token,
                body={"action": action},
                note=f"B {action}",
            )
            check(f"{action} 成功且状态 {expect_status}",
                  status == 200 and (step.get("data") or {}).get("status") == expect_status,
                  f"status={(step.get('data') or {}).get('status')}")

        status, bad = api.call(
            "POST", f"/api/v1/orders/{order_id}/progress", token=a_token, body={"action": "PICKED_UP"},
            note="发单人推进 → 应 403/422",
        )
        check("发单人不能推进接单人节点（4xx）", 400 <= status < 500, f"HTTP {status}")

        # --------------------------------------------------------- 确认收货
        hr("8. 确认收货（T9：DELIVERED → COMPLETED）")
        status, runner_confirm = api.call(
            "POST", f"/api/v1/orders/{order_id}/confirm", token=b_token, note="接单人确认 → 应 403"
        )
        check("接单人不能确认收货（4xx）", 400 <= status < 500, f"HTTP {status}")

        status, confirmed = api.call(
            "POST", f"/api/v1/orders/{order_id}/confirm", token=a_token, note="A 确认收货"
        )
        check("确认收货成功且状态 COMPLETED",
              status == 200 and (confirmed.get("data") or {}).get("status") == "COMPLETED",
              f"status={(confirmed.get('data') or {}).get('status')}")

        status, final = api.call("GET", f"/api/v1/orders/{order_id}", token=a_token, note="终态详情")
        final_status = (final.get("data") or {}).get("status")
        check("终态为 COMPLETED", final_status == "COMPLETED", f"status={final_status}")
        # `COMPLETED` **不是**终态：BR-28 允许完成后 48h 内发起投诉（T12），
        # 所以发单人在这个状态下唯一还能做的动作就是「发起投诉」。
        # 若这里出现 CONFIRM / PICKED_UP 之类，说明 available_actions 的
        # 「状态×角色」过滤算错了（前端会渲染出点了必报错的按钮）。
        check(
            "终态只剩「发起投诉」（BR-28 的 48h 窗口）",
            (final.get("data") or {}).get("available_actions") == ["DISPUTE_OPENED"],
            f"actions={(final.get('data') or {}).get('available_actions')}",
        )

    hr("结果汇总")
    failed = [item for item in _checks if not item[1]]
    print(f"  共 {len(_checks)} 项断言，通过 {len(_checks) - len(failed)} 项，失败 {len(failed)} 项")
    for label, _ok, detail in failed:
        print(f"  [FAIL] {label}  {detail}")
    print()
    print("  链路：注册 → 发单 → 大厅 → 抢单 → 取件/配送/送达 → 确认收货  全部走通 ✓"
          if not failed else "  链路未走通 ✗")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
