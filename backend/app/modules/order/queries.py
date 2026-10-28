"""订单**读侧**查询（大厅 / 我的订单 / 详情 / 时间轴）：CQRS 里的 Q。

## 为什么单独一个文件，而不是继续塞 `repo.py`

`repo.py` 是**状态机的写侧仓储**（`append_progress` / `cas_update_status`），
它的每个方法都在"一个请求一个事务"里承担并发正确性。读侧查询与它没有共享不变量：
- 读侧可以被缓存、可以加 `LIMIT`、可以随便改字段；
- 写侧一改就可能破坏 CAS / 投影对账。

混在一起的后果是：为了给详情页加一个 `poi_name`，改动落到一个被几十个集成用例
盯着的文件里，diff 评审成本陡增。分开之后，`repo.py` 保持"只写状态"的窄语义。

## 三条硬规则（都在本文件里落实）

1. **列表绝不 `SELECT pick_code_enc`**。不是"查出来再过滤"—— 那样一个 `logger.info(row)`
   就泄露了。列清单是白名单，密文列根本不在 SQL 里（`_HALL_SQL` / `_MINE_SQL` 都不含它）。
2. **详情页的明文取件码只对当事人可见**：发单人，或**已抢到该单**的接单人。
   未抢单的接单人看到 `None` —— 否则"抢单"这道门槛形同虚设。
3. **字段名跟消费方对齐**：前端（`web/src/types.ts`）用的是 `publisher_name` /
   `runner_name` / `my_role` / `timeline`，这里就按这些名字下发。库里叫 `nickname`
   是库的事，DTO 要按使用者的语言说话 —— 否则前端只能靠猜（然后就猜错了，
   `publisher_name` 变成 `undefined`，页面上显示"同学"）。

## 大厅/我的订单为什么过滤 `deadline > now`

`T4`（到期 → EXPIRED）是系统任务，M1 没有调度器。若不过滤，大厅会把一堆
"已经过期但还没被扫走"的单子展示给接单人，点进去必然 `42201`（`guard_grab` 的
过期判定）—— 用户会认为"抢单功能坏了"。读侧过滤是**展示口径**，不改任何状态。

> 注意"我的订单"**不过滤过期**：我自己发的过期单也必须在我的列表里出现，
> 否则用户会以为单子凭空消失了。
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.clock import clock as default_clock
from app.core.errors import ApiError, ErrCode
from app.core.security import decrypt_pick_code
from app.modules.order.state_machine import (
    Action,
    Actor,
    OrderCtx,
    Status,
    Thresholds,
    available_actions,
)

logger = logging.getLogger("campusrun.order.read")

#: 大厅/我的订单的单页上限。本轮不做游标分页（K-05 的 `Page` 留给后续迭代）：
#: 校园场景同时待接单在几十量级，`LIMIT 50` 一次拿完比引入游标更简单。
HALL_LIMIT: Final[int] = 50

#: 接单人能看到明文取件码的订单状态（= 他已经抢到这一单）。
RUNNER_PICK_CODE_STATUSES: Final[frozenset[str]] = frozenset(
    {Status.IN_PROGRESS.value, Status.DELIVERED.value, Status.COMPLETED.value}
)

#: 大厅/我的订单共用的列白名单 —— **没有 `pick_code_enc`**（见模块文档第 1 条）。
_LIST_COLUMNS: Final[str] = """
    o.id, o.order_no, o.type, o.status,
    o.from_poi_id, fp.name AS from_poi_name,
    o.to_poi_id,   tp.name AS to_poi_name,
    o.reward, o.deadline, o.created_at, o.expect_minutes, o.description,
    o.publisher_id, o.runner_id,
    pu.nickname AS publisher_name,
    ru.nickname AS runner_name
"""

#: 列表用的 JOIN 段（两个 POI 字典 + 双方昵称）。写成常量是为了让
#: "大厅"与"我的订单"两条 SQL 的列清单**只可能有一份**（漏改一处就是字段缺失）。
_LIST_JOINS: Final[str] = """
FROM orders o
JOIN pois fp ON fp.id = o.from_poi_id
JOIN pois tp ON tp.id = o.to_poi_id
LEFT JOIN users pu ON pu.id = o.publisher_id
LEFT JOIN users ru ON ru.id = o.runner_id
"""

#: 大厅：只要 PENDING 且未过期。
_HALL_SQL: Final[str] = f"""
SELECT {_LIST_COLUMNS}
{_LIST_JOINS}
WHERE o.status = :status
  AND o.deadline > :now
ORDER BY o.created_at DESC, o.id DESC
LIMIT :limit
"""

#: 我的订单：我发的 **或** 我接的。不过滤过期（自己发的过期单也要看得见）。
#: `OR` 走不了 `(publisher_id, created_at)` 索引 —— 但这个接口天然按人查、
#: 单页 50 条，PG 会走 BitmapOr + 两条索引，代价可接受；拆成两条 SQL 再合并
#: 反而要在应用层排序，得不偿失。
_MINE_SQL: Final[str] = f"""
SELECT {_LIST_COLUMNS}
{_LIST_JOINS}
WHERE o.publisher_id = :user_id OR o.runner_id = :user_id
ORDER BY o.created_at DESC, o.id DESC
LIMIT :limit
"""

#: 详情：一次查询拿全（含两个 POI 名与双方昵称），避免详情页 N+1。
_DETAIL_SQL: Final[str] = """
SELECT
    o.id, o.order_no, o.status, o.type,
    o.publisher_id, o.runner_id,
    o.from_poi_id, fp.name AS from_poi_name,
    o.to_poi_id,   tp.name AS to_poi_name,
    o.reward, o.description, o.images, o.pick_code_enc, o.expect_minutes,
    o.deadline, o.created_at, o.accepted_at, o.delivered_at, o.completed_at,
    o.auto_confirm_due_at, o.is_late,
    pu.nickname AS publisher_name,
    ru.nickname AS runner_name
FROM orders o
JOIN pois fp ON fp.id = o.from_poi_id
JOIN pois tp ON tp.id = o.to_poi_id
LEFT JOIN users pu ON pu.id = o.publisher_id
LEFT JOIN users ru ON ru.id = o.runner_id
WHERE o.id = :order_id
"""

#: 时间轴：只取"渲染时间轴要的四个字段"，不整个事件行拉回来。
_TIMELINE_SQL: Final[str] = """
SELECT action, actor_role, created_at
FROM order_progress
WHERE order_id = :order_id
ORDER BY seq
"""

#: 详情页时间轴的 6 个固定节点（顺序即页面从上到下的顺序）。
#: 与前端 `web/src/lib/format.ts::fallbackTimeline` 的 key/label **逐字一致**：
#: 后端下发时前端直接用，后端没下发时前端按 status 反推 —— 两条路的 key 一样，
#: 才不会出现"有时显示中文、有时显示 CREATE"。
TIMELINE_STEPS: Final[tuple[tuple[str, str], ...]] = (
    ("CREATE", "发单"),
    ("ACCEPT", "接单"),
    ("PICKED_UP", "已取件"),
    ("ON_THE_WAY", "配送中"),
    ("DELIVER", "已送达"),
    ("CONFIRM", "完成"),
)


@dataclass(frozen=True, slots=True)
class HallItem:
    """列表条目（大厅 / 我的订单共用；只含公开字段，无任何密文）。"""

    id: int
    order_no: str
    type: str
    status: str
    from_poi_id: int
    from_poi_name: str
    to_poi_id: int
    to_poi_name: str
    reward: Decimal
    deadline: datetime.datetime
    created_at: datetime.datetime
    expect_minutes: int
    description: str | None
    publisher_id: int
    runner_id: int | None
    publisher_name: str | None
    runner_name: str | None

    def to_dict(
        self,
        *,
        actions: tuple[Action, ...],
        my_role: str | None = None,
    ) -> dict[str, Any]:
        """列表项投影。

        `actions` 由调用方按"当前用户角色"注入（不在这里猜身份）；
        `my_role` 只在 `/orders/mine` 下发（前端优先用它，不再靠 `publisher_id` 推断）。
        """
        data: dict[str, Any] = {
            "id": self.id,
            "order_no": self.order_no,
            "type": self.type,
            "status": self.status,
            "from_poi_id": self.from_poi_id,
            "from_poi_name": self.from_poi_name,
            "to_poi_id": self.to_poi_id,
            "to_poi_name": self.to_poi_name,
            "reward": self.reward,
            "deadline": self.deadline,
            "created_at": self.created_at,
            "expect_minutes": self.expect_minutes,
            "description": self.description,
            "publisher_id": self.publisher_id,
            "publisher_name": self.publisher_name,
            "runner_id": self.runner_id,
            "runner_name": self.runner_name,
            "available_actions": [a.value for a in actions],
        }
        if my_role is not None:
            data["my_role"] = my_role
        return data


@dataclass(frozen=True, slots=True)
class OrderDetail:
    """订单详情行（含 `pick_code_enc`，是否解密由可见性规则决定）。"""

    id: int
    order_no: str
    status: str
    type: str
    publisher_id: int
    runner_id: int | None
    from_poi_id: int
    from_poi_name: str
    to_poi_id: int
    to_poi_name: str
    reward: Decimal
    description: str | None
    images: tuple[str, ...]
    pick_code_enc: bytes | None
    expect_minutes: int
    deadline: datetime.datetime
    created_at: datetime.datetime
    accepted_at: datetime.datetime | None
    delivered_at: datetime.datetime | None
    completed_at: datetime.datetime | None
    auto_confirm_due_at: datetime.datetime | None
    is_late: bool
    publisher_name: str | None
    runner_name: str | None

    def to_dict(
        self,
        *,
        pick_code: str | None,
        actions: tuple[Action, ...],
        timeline: tuple[dict[str, Any], ...] = (),
    ) -> dict[str, Any]:
        """详情响应体。

        `pick_code=None` 时 `pick_code_masked=True`（前端显示 `****`）；
        `timeline` 是 **6 个固定节点**（未发生的节点 `at=null, done=false`），
        前端有兜底但优先用后端下发的这份（它带真实时刻）。
        """
        return {
            "id": self.id,
            "order_no": self.order_no,
            "status": self.status,
            "type": self.type,
            "publisher_id": self.publisher_id,
            "runner_id": self.runner_id,
            "from_poi_id": self.from_poi_id,
            "from_poi_name": self.from_poi_name,
            "to_poi_id": self.to_poi_id,
            "to_poi_name": self.to_poi_name,
            "reward": self.reward,
            "description": self.description,
            "images": list(self.images),
            "expect_minutes": self.expect_minutes,
            "deadline": self.deadline,
            "created_at": self.created_at,
            "accepted_at": self.accepted_at,
            "delivered_at": self.delivered_at,
            "completed_at": self.completed_at,
            "auto_confirm_due_at": self.auto_confirm_due_at,
            "is_late": self.is_late,
            "publisher_name": self.publisher_name,
            "runner_name": self.runner_name,
            "pick_code": pick_code,
            "pick_code_masked": pick_code is None,
            "available_actions": [a.value for a in actions],
            "timeline": list(timeline),
        }


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def _row_to_item(row: Any) -> HallItem:
    """把一条查询结果映射成 `HallItem`（大厅与我的订单共用同一个映射）。"""
    return HallItem(
        id=int(row["id"]),
        order_no=str(row["order_no"]),
        type=str(row["type"]),
        status=str(row["status"]),
        from_poi_id=int(row["from_poi_id"]),
        from_poi_name=str(row["from_poi_name"]),
        to_poi_id=int(row["to_poi_id"]),
        to_poi_name=str(row["to_poi_name"]),
        reward=Decimal(str(row["reward"])),
        deadline=row["deadline"],
        created_at=row["created_at"],
        expect_minutes=int(row["expect_minutes"]),
        description=row["description"],
        publisher_id=int(row["publisher_id"]),
        runner_id=int(row["runner_id"]) if row["runner_id"] is not None else None,
        publisher_name=row["publisher_name"],
        runner_name=row["runner_name"],
    )


def list_hall(
    db: Session,
    *,
    now: datetime.datetime | None = None,
    limit: int = HALL_LIMIT,
) -> list[HallItem]:
    """大厅：`PENDING` 且未过期的订单，按发布时间倒序。

    Args:
        db: 请求级会话（只读）。
        now: 观察时刻（默认进程时钟，UTC aware）。测试注入固定时钟。
        limit: 条数上限，默认 50。

    Returns:
        大厅条目列表（空列表表示"暂时没有可接的单"，不是错误）。
    """
    moment = default_clock.to_utc(now) if now is not None else default_clock.now()
    rows = db.execute(
        sa.text(_HALL_SQL),
        {
            "status": Status.PENDING.value,
            "now": moment,
            "limit": max(1, int(limit)),
        },
    ).mappings().all()
    return [_row_to_item(row) for row in rows]


def list_mine(db: Session, *, user_id: int, limit: int = HALL_LIMIT) -> list[HallItem]:
    """我的订单：`publisher_id = 我` **或** `runner_id = 我`，按发布时间倒序。

    与大厅的两处差异都是刻意的：
    1. **不过滤 `deadline`**：我自己发的单过期了也得留在列表里（否则用户以为丢了）；
    2. **不限 `status`**：`COMPLETED` / `CANCELLED` 都是我要回看的记录。
    """
    rows = db.execute(
        sa.text(_MINE_SQL),
        {"user_id": user_id, "limit": max(1, int(limit))},
    ).mappings().all()
    return [_row_to_item(row) for row in rows]


def load_detail(db: Session, order_id: int) -> OrderDetail:
    """订单详情。不存在 → `404/40401`（与 `load_ctx` 同码，前端只需一套处理）。"""
    row = db.execute(sa.text(_DETAIL_SQL), {"order_id": order_id}).mappings().first()
    if row is None:
        raise ApiError(404, ErrCode.ORDER_NOT_FOUND, "订单不存在")
    return OrderDetail(
        id=int(row["id"]),
        order_no=str(row["order_no"]),
        status=str(row["status"]),
        type=str(row["type"]),
        publisher_id=int(row["publisher_id"]),
        runner_id=int(row["runner_id"]) if row["runner_id"] is not None else None,
        from_poi_id=int(row["from_poi_id"]),
        from_poi_name=str(row["from_poi_name"]),
        to_poi_id=int(row["to_poi_id"]),
        to_poi_name=str(row["to_poi_name"]),
        reward=Decimal(str(row["reward"])),
        description=row["description"],
        images=tuple(row["images"] or ()),
        pick_code_enc=row["pick_code_enc"],
        expect_minutes=int(row["expect_minutes"]),
        deadline=row["deadline"],
        created_at=row["created_at"],
        accepted_at=row["accepted_at"],
        delivered_at=row["delivered_at"],
        completed_at=row["completed_at"],
        auto_confirm_due_at=row["auto_confirm_due_at"],
        is_late=bool(row["is_late"]),
        publisher_name=row["publisher_name"],
        runner_name=row["runner_name"],
    )


def build_timeline(
    db: Session,
    order_id: int,
    *,
    status: str,
) -> tuple[dict[str, Any], ...]:
    """详情页时间轴：6 个固定节点 + 每个节点的真实发生时刻。

    ## 为什么是"固定 6 节点"而不是"事件流原样下发"

    前端要的是一条竖直时间轴：未来节点也要占位（灰色、"待完成"）。若只下发
    已发生的事件，前端得自己补齐剩余节点 —— 那就是把状态机知识复制到前端。
    这里按 `TIMELINE_STEPS` 补齐，`at=None, done=False` 表示"还没发生"。

    ## `current` 的口径

    `current` = **最后一个已完成的节点**（页面高亮那一行）。`COMPLETED` 时全节点
    都是 `done`，按前端的约定不再标 `current`（否则最后一行会同时是"完成"和"进行中"）。

    同一个 action 出现多次（例如重复 `NOTE` 不算在内，但理论上 `ACCEPT` 只会有一条）
    时取**最后一次**的时刻 —— 时间轴要显示"最终发生的时间"。
    """
    rows = db.execute(sa.text(_TIMELINE_SQL), {"order_id": order_id}).all()
    at_by_action: dict[str, datetime.datetime] = {}
    for action, _role, created_at in rows:
        at_by_action[str(action)] = created_at

    last_done = max(
        (index for index, (key, _label) in enumerate(TIMELINE_STEPS) if key in at_by_action),
        default=-1,
    )
    nodes: list[dict[str, Any]] = []
    for index, (key, label) in enumerate(TIMELINE_STEPS):
        at = at_by_action.get(key)
        nodes.append(
            {
                "key": key,
                "label": label,
                "at": at,
                "done": at is not None,
                "current": index == last_done and status != Status.COMPLETED.value,
            }
        )
    return tuple(nodes)


# ---------------------------------------------------------------------------
# 可见性 / 投影
# ---------------------------------------------------------------------------
def pick_code_visible_to(detail: OrderDetail, user_id: int) -> bool:
    """明文取件码的可见性判据（FR-ORDER-10）。

    - 发单人本人：永远可见（这本来就是他自己填的码）；
    - 接单人：**只有已抢到该单**（`runner_id == 我` 且状态已离开 PENDING）才可见；
    - 其他人（含还没抢单的接单人）：不可见。

    第二条的"且状态已离开 PENDING"在数据上冗余（`runner_id` 非空即说明抢过），
    但它把不变量写成了一句可读的话：**拿到明文的前提是"这单已经在我手上"**。
    """
    if user_id == detail.publisher_id:
        return True
    if detail.runner_id == user_id and detail.status in RUNNER_PICK_CODE_STATUSES:
        return True
    return False


def reveal_pick_code(detail: OrderDetail, user_id: int) -> str | None:
    """按可见性规则返回明文取件码；不可见或解不开都返回 `None`。

    解密失败（密钥轮换 / 密文损坏）**不让详情页 500**：一个已完成的单子
    因为历史密钥对不上而整页打不开，比"取件码这一栏显示 `****`"糟糕得多。
    失败走 `logger.error`，让它在监控里可见。
    """
    if detail.pick_code_enc is None:
        return None
    if not pick_code_visible_to(detail, user_id):
        return None
    try:
        return decrypt_pick_code(detail.pick_code_enc)
    except ValueError:
        logger.error("order.detail.pick_code_decrypt_failed order_id=%s", detail.id)
        return None


def actor_for(ctx: OrderCtx, user_id: int, *, is_verified: bool = True) -> Actor:
    """把"当前登录用户"翻译成状态机 `Actor`（角色由**关系**决定，不由客户端声明）。

    规则只有两条：
    - `user_id == ctx.publisher_id` → `PUBLISHER`；
    - 其余一切人 → `RUNNER`（"潜在的接单人"）。

    ## 为什么非当事人一律按 RUNNER 处理，而不是"按想做的动作猜"

    早先的实现是「`ctx.runner_id == user_id` 才给 RUNNER，否则给 PUBLISHER」，
    它有一个反直觉的后果：**待接单的单子 `runner_id` 是 NULL**，于是去看大厅/
    详情的接单人全被当成了发单人 —— `available_actions` 对 `PENDING` 返回
    `["CANCEL"]` 而不是 `["ACCEPT"]`，前端会渲染出"取消别人的单"这个按钮。

    判据必须只依赖**身份关系**，不依赖意图：
    - 发单人永远是 `PUBLISHER`；
    - 其他人即使不是当事人，也只能是"接单侧"，越权由守卫兜底
      （`_node_in_order` 要求 `actor.id == ctx.runner_id`，T9 只放行 PUBLISHER，
      所以陌生人拿到 RUNNER 身份也做不了任何事，只会得到 42201 / 40310）。

    刻意不接收 `role` 参数：让客户端挑角色等于把越权入口开在最前面。
    """
    if ctx.publisher_id == user_id:
        return Actor.publisher(user_id, is_verified=is_verified)
    return Actor.runner(user_id, is_verified=is_verified)


def my_role_of(item: HallItem, user_id: int) -> str:
    """我在这一单里的角色（`/orders/mine` 专用）。

    同一单既可能是我发的、也可能是我接的（理论上不会，但历史数据可能），
    此时按 `PUBLISHER` 优先 —— 发单人的动作集更大（能取消、能确认收货）。
    """
    return "PUBLISHER" if item.publisher_id == user_id else "RUNNER"


def detail_actions(
    ctx: OrderCtx,
    actor: Actor,
    *,
    now: datetime.datetime,
    cfg: Thresholds,
) -> tuple[Action, ...]:
    """详情页的可用动作：**守卫层精度**（返回的每个按钮点了真的会成功）。

    ## 为什么详情必须用守卫层

    只做角色层过滤会返回"点了必失败"的按钮：接单人已经 `PICKED_UP` 之后，
    仍会返回 `PICKED_UP`（再点必 `42201`）和 `DELIVER`（跳级必拒）。
    前端按后端给的按钮渲染，用户就点出一个"操作失败" —— 这不是权限问题，
    是**契约不诚实**。录屏里最刺眼的一幕就是"点完已取件，按钮还在，再点报错"。

    守卫层之所以几乎零成本：本项目所有守卫都是 `(ctx, actor, now, cfg, action)`
    的**纯函数**（不查库），`OrderCtx` 已经把 `deadline` / `last_node` /
    `has_runner_evidence` / `dispute_count` / `inflight` 都带全了。

    ## `now` / `cfg` 为什么是必填而不是默认空

    默认 `now=None` 会静默退回角色层 —— 调用方少传一个参数，bug 就无声无息地
    回来了（这正是上一版详情页的问题）。必填 + 显式从 service 传下来，
    让"忘了启用守卫层"变成 TypeError 而不是线上事故。

    ## 前置条件

    `ctx` 必须是 `repo.load_ctx(order_id, viewer_id=actor.id)` 的结果 ——
    `guard_grab` 读的 `runner_inflight_count` 要按**谁在看**算，
    否则拿"本单接单人的在途数"判"当前用户能不能接"，`ACCEPT` 会算错。

    Args:
        ctx: 已按 viewer 口径加载的订单快照（**不要**在这里再查一次库）。
        actor: 当前登录用户的角色投影。
        now: 观察时刻（UTC aware）。
        cfg: 运行期阈值（来自 `ConfigService`；运营改过之后默认值就不对了）。
    """
    return available_actions(ctx, actor, now=now, cfg=cfg)


__all__ = [
    "HALL_LIMIT",
    "RUNNER_PICK_CODE_STATUSES",
    "TIMELINE_STEPS",
    "HallItem",
    "OrderDetail",
    "actor_for",
    "build_timeline",
    "detail_actions",
    "list_hall",
    "list_mine",
    "load_detail",
    "my_role_of",
    "pick_code_visible_to",
    "reveal_pick_code",
]
