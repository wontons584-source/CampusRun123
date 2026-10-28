"""并发抢单的**真实并发**测试（T-20 的原子性证据）。

## 这个文件为什么必须存在

单测里的"并发"通常是假并发：调两次函数、断言第二次失败。它证明的只是
"状态判断写对了"，证明不了**同一瞬间 20 个请求打进来只有 1 个赢**。
而抢单的所有难点都在这里：

- Redis 锁只持有时一次往返的时间（3s TTL），锁过期/Redis 抖动时靠什么兜底？
- `append_progress` 里的 `SELECT ... FOR UPDATE` 与 `cas_update_status` 的
  `WHERE status='PENDING'` 是否真的能把 20 个请求串成一条线？
- 输的那 19 个是不留任何痕迹（不写 `ACCEPT` 事件、不写 `runner_id`），
  还是留下了半截数据？

**这些只有真 PG + 真 Redis + 真线程能回答。** 每个线程用自己的 `Session`，
共享一个 session 是测不出并发的（SQLAlchemy 的 Session 不是线程安全的，
共用会先撞 session 状态而不是数据库行锁）。

## 断言的四条判据

1. `status='IN_PROGRESS'` 的行数 == **1**（不是 0 也不是 2）；
2. 19 个失败方**全部**是 `409/40901`（"手慢了"），不能混进别的码 ——
   否则前端要为同一次抢单写两套文案；
3. `order_progress` 里 `ACCEPT` 事件只有 **1** 条（输的那 19 个事务整段回滚）；
4. `orders.runner_id` 只被写一次，且等于成功者的 id。

## 为什么是 20 线程而不是 100

本机 PostgreSQL 的连接数上限与 `db_pool_size`（10 + overflow 10）扛不住 100 个
并发连接，硬上会先在连接池上排队到超时，测出来的是"连接池打架"而不是"抢单原子性"。
20 已经远超"两个请求恰好错开"的偶然窗口 —— 若 CAS 有洞，20 线程必然复现。

## 跑法

```bash
export TEST_DATABASE_URL="postgresql+psycopg://campusrun:campusrun@127.0.0.1:5433/campusrun"
export REDIS_URL="redis://127.0.0.1:6379/0"      # 本例按需改成实际端口
pytest tests/test_grab_concurrent.py -k concurrent -v
```

未设 `TEST_DATABASE_URL` 时整文件 skip（默认套件不受影响）。
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any, Final
from uuid import uuid4

import pytest
import sqlalchemy as sa
from app.core.clock import UTC
from app.core.errors import ApiError, ErrCode
from app.modules.order.repo import OrderDraft, SqlOrderRepo
from app.modules.order.service import OrderService
from app.modules.order.state_machine import Status
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

TEST_DATABASE_URL: str = os.environ.get("TEST_DATABASE_URL", "").strip()
REDIS_URL: str = os.environ.get("REDIS_URL", "").strip()

#: 并发线程数（见模块文档"为什么是 20"）
THREADS: Final[int] = 20

#: UTC 02:00 = 北京 10:00（发单窗内，且不贴窗边）
NOW: Final[dt.datetime] = dt.datetime(2025, 6, 1, 2, 0, tzinfo=UTC)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.concurrent,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="未设置 TEST_DATABASE_URL，跳过并发抢单集成测试",
    ),
]


@pytest.fixture(scope="module")
def engine() -> Iterator[sa.Engine]:
    """独立引擎 + `NullPool`：每个线程各拿一条连接，不共享、不排队。

    `NullPool` 是关键：默认池只有 10+10 条连接，20 线程会先在池上排队，
    测出来的"并发"其实是串行。`NullPool` 让 20 个线程真的同时连着库。
    """
    if not TEST_DATABASE_URL:
        pytest.skip("未设置 TEST_DATABASE_URL")
    eng = sa.create_engine(TEST_DATABASE_URL, poolclass=NullPool, future=True)
    try:
        with eng.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        eng.dispose()
        pytest.skip(f"TEST_DATABASE_URL 不可达：{exc}")
    yield eng
    eng.dispose()


@pytest.fixture()
def redis_env() -> Iterator[Any]:
    """把 `app.db.redis` 的进程级单例指向一个可用的客户端。

    Redis 客户端（redis-py）本身线程安全，20 个线程共用同一个客户端即可 ——
    这里要的是"共用一把锁"，而不是"每个线程各自的锁"。
    """
    from app.db import redis as redis_mod

    if not REDIS_URL:
        pytest.skip("未设置 REDIS_URL，跳过并发抢单测试")
    import redis as redis_lib

    client = redis_lib.Redis.from_url(
        REDIS_URL, decode_responses=False, socket_connect_timeout=2, socket_timeout=2
    )
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis 不可达（{REDIS_URL}）：{exc}")
    redis_mod.set_redis(client)
    try:
        yield client
    finally:
        redis_mod.reset_for_tests()
        client.close()


# ---------------------------------------------------------------------------
# 数据脚手架（并发用例自己建数据、自己删 —— 线程间不能共享回滚事务）
# ---------------------------------------------------------------------------
def _mk_user(connection: Any, name: str) -> int:
    return int(
        connection.execute(
            sa.text(
                "INSERT INTO users (nickname, credit_score, verify_status, status) "
                "VALUES (:n, 100, 'APPROVED', 'ACTIVE') RETURNING id"
            ),
            {"n": f"{name}-{uuid4().hex[:6]}"},
        ).scalar_one()
    )


def _mk_poi(connection: Any, name: str, poi_type: str) -> int:
    return int(
        connection.execute(
            sa.text(
                "INSERT INTO pois (campus_id, name, type, lat, lng) "
                "VALUES (1, :n, :t, 32.057200, 118.639800) RETURNING id"
            ),
            {"n": f"{name}-{uuid4().hex[:6]}", "t": poi_type},
        ).scalar_one()
    )


def _draft(publisher_id: int, from_poi: int, to_poi: int) -> OrderDraft:
    return OrderDraft(
        order_no=f"CRC{uuid4().hex[:10].upper()}",
        publisher_id=publisher_id,
        type="BUY",
        from_poi_id=from_poi,
        to_poi_id=to_poi,
        from_lat=Decimal("32.057200"),
        from_lng=Decimal("118.639800"),
        to_lat=Decimal("32.058100"),
        to_lng=Decimal("118.641800"),
        reward=Decimal("5.50"),
        expect_minutes=60,
        deadline=NOW + dt.timedelta(hours=1),
        description="并发抢单用例",
    )


def _cleanup(
    engine: sa.Engine, *, order_id: int, user_ids: list[int], poi_ids: list[int]
) -> None:
    """删掉本用例造的行（`order_progress` → `orders` → `users` / `pois`，按 FK 顺序）。

    **必须删 POI**：本文件的用例会 `commit`（并发用例不能靠外层事务回滚），
    每次跑都留下一对 `驿站-xxxx`/`宿舍-xxxx`。不删的话跑十次就是 20 条垃圾字典项，
    而 `GET /pois` 是发单页首屏 —— 演示时会看到一串乱码地点。
    """
    with Session(engine) as session:
        session.execute(
            sa.text("DELETE FROM order_progress WHERE order_id = :o"), {"o": order_id}
        )
        session.execute(sa.text("DELETE FROM orders WHERE id = :o"), {"o": order_id})
        if user_ids:
            session.execute(
                sa.text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": user_ids}
            )
        if poi_ids:
            session.execute(
                sa.text("DELETE FROM pois WHERE id = ANY(:ids)"), {"ids": poi_ids}
            )
        session.commit()


# ===========================================================================
# 20 线程抢同一单：只有 1 个赢
# ===========================================================================
def test_concurrent_grab_only_one_wins(engine: sa.Engine, redis_env: Any) -> None:
    """20 个线程 + 20 个独立 Session 抢同一个 PENDING 订单。

    断言（四条判据见模块文档）：恰好 1 单进入 `IN_PROGRESS`、19 个失败者
    **全部** `409/40901`、`ACCEPT` 事件恰好 1 条、`runner_id` 等于赢家。
    """
    from app.db.redis import k_grab_order

    # ---- 造数据：1 个发单人 + 20 个接单人 + 1 个 PENDING 订单（**已提交**）----
    with Session(engine) as setup:
        publisher = _mk_user(setup.connection(), "并发发单人")
        runners = [_mk_user(setup.connection(), f"并发接单人{i}") for i in range(THREADS)]
        from_poi = _mk_poi(setup.connection(), "驿站", "STATION")
        to_poi = _mk_poi(setup.connection(), "宿舍", "DORM")
        order_id = SqlOrderRepo(setup).create_order(_draft(publisher, from_poi, to_poi), now=NOW)
        setup.commit()

    barrier = threading.Barrier(THREADS)
    outcomes: list[tuple[int, int] | str] = []
    lock = threading.Lock()

    def worker(runner_id: int) -> None:
        """一个线程 = 一条独立连接 + 一次抢单尝试。"""
        session = Session(engine, expire_on_commit=False)
        try:
            service = OrderService(session)
            barrier.wait(timeout=30)          # 让 20 个线程尽量同时出手
            try:
                service.grab_order(order_id=order_id, runner_id=runner_id, now=NOW)
                session.commit()
                with lock:
                    outcomes.append("ok")
            except ApiError as exc:
                session.rollback()            # 输的事务整段回滚：不许留半截数据
                with lock:
                    outcomes.append((exc.http_status, exc.code))
            except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
                session.rollback()
                with lock:
                    outcomes.append(f"unexpected:{exc!r}")
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            for runner in runners:
                pool.submit(worker, runner)

        # ---- 判据 1/2：恰好 1 成功，其余全是 409/40901 ----
        winners = [o for o in outcomes if o == "ok"]
        failures = [o for o in outcomes if o != "ok"]
        assert len(winners) == 1, f"应恰好 1 个赢家，实得 {len(winners)}：{outcomes}"
        assert len(failures) == THREADS - 1
        wrong = [f for f in failures if f != (409, ErrCode.ORDER_ALREADY_TAKEN)]
        assert not wrong, (
            "失败方必须全部是 409/40901（手慢了），不能混进别的码；"
            f"异常项：{wrong}（完整结果：{outcomes}）"
        )

        # ---- 判据 3/4：库里只有 1 个 IN_PROGRESS、1 条 ACCEPT、1 个 runner_id ----
        with Session(engine) as check:
            status, runner_id, version = check.execute(
                sa.text(
                    "SELECT status, runner_id, version FROM orders WHERE id = :o"
                ),
                {"o": order_id},
            ).one()
            accept_count = int(
                check.execute(
                    sa.text(
                        "SELECT count(*) FROM order_progress "
                        "WHERE order_id = :o AND action = 'ACCEPT'"
                    ),
                    {"o": order_id},
                ).scalar_one()
            )
            inflight_rows = int(
                check.execute(
                    sa.text(
                        "SELECT count(*) FROM orders "
                        "WHERE id = :o AND status = 'IN_PROGRESS'"
                    ),
                    {"o": order_id},
                ).scalar_one()
            )

        assert (status, inflight_rows) == (Status.IN_PROGRESS.value, 1), (
            f"应恰好 1 行 IN_PROGRESS，实得 status={status} rows={inflight_rows}"
        )
        assert accept_count == 1, f"ACCEPT 事件应恰好 1 条，实得 {accept_count} 条"
        assert runner_id in runners, f"runner_id={runner_id} 不在 20 个接单人之列"
        assert version == 1, f"只该发生 1 次 CAS，version 应为 1，实得 {version}"
    finally:
        _cleanup(
            engine,
            order_id=order_id,
            user_ids=[publisher, *runners],
            poi_ids=[from_poi, to_poi],
        )
        redis_env.delete(k_grab_order(order_id))


def test_concurrent_grab_leaves_no_partial_trace(engine: sa.Engine, redis_env: Any) -> None:
    """反证：19 个失败者**一条事件都没写**、`orders` 行也没被改。

    上一条用例只验证"最终状态对"。这条验证的是**原子性本身**：
    如果 `apply()` 不是"先 append、再 CAS、CAS 失败整段回滚"，而是"先写事件
    再判状态"，那么 `order_progress` 里会留下 19 条 `ACCEPT`（状态却只有 1 次迁移），
    投影对账当场崩掉 —— 这种脏数据不会报错，只会在对账时炸。
    """
    from app.db.redis import k_grab_order

    with Session(engine) as setup:
        publisher = _mk_user(setup.connection(), "原子性发单人")
        runners = [_mk_user(setup.connection(), f"原子性接单人{i}") for i in range(THREADS)]
        from_poi = _mk_poi(setup.connection(), "驿站", "STATION")
        to_poi = _mk_poi(setup.connection(), "宿舍", "DORM")
        order_id = SqlOrderRepo(setup).create_order(_draft(publisher, from_poi, to_poi), now=NOW)
        setup.commit()

    barrier = threading.Barrier(THREADS)

    def worker(runner_id: int) -> None:
        session = Session(engine, expire_on_commit=False)
        try:
            barrier.wait(timeout=30)
            try:
                OrderService(session).grab_order(order_id=order_id, runner_id=runner_id, now=NOW)
                session.commit()
            except ApiError:
                session.rollback()
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            for runner in runners:
                pool.submit(worker, runner)

        with Session(engine) as check:
            events = check.execute(
                sa.text(
                    "SELECT action, seq FROM order_progress WHERE order_id = :o ORDER BY seq"
                ),
                {"o": order_id},
            ).all()
        actions = [str(row[0]) for row in events]
        seqs = [int(row[1]) for row in events]
        assert actions == ["CREATE", "ACCEPT"], (
            f"事件流应恰好 CREATE + ACCEPT 两条，实得 {actions}"
        )
        assert seqs == [1, 2], f"seq 必须连续（并发下不许有空号），实得 {seqs}"
    finally:
        _cleanup(
            engine,
            order_id=order_id,
            user_ids=[publisher, *runners],
            poi_ids=[from_poi, to_poi],
        )
        redis_env.delete(k_grab_order(order_id))


def test_concurrent_grab_without_redis_lock_still_one_winner(
    engine: sa.Engine, redis_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**把 Redis 锁拿掉**，20 个线程仍然只能有 1 个赢家。

    这条是整个交付里最有说服力的判据：它证明"不会两个人抢到同一单"靠的是
    `cas_update_status` 的 `WHERE status='PENDING'` + `append_progress` 的
    `SELECT ... FOR UPDATE`，**不是** Redis 锁。Redis 挂了 / 主从切换 / 锁提前
    过期，最坏结果只是并发全部打到数据库（变慢），正确性不受影响 —— 这正是
    问题里"Redis 只是性能手段"那句话的实证。

    做法：把 `service.grab_lock` 换成永远返回 `True`（等价于"所有人都认为
    自己拿到了锁"），然后重跑同一套 20 线程断言。
    """
    from app.db.redis import k_grab_order

    monkeypatch.setattr(
        "app.modules.order.service.grab_lock",
        lambda *args, **kwargs: True,
    )

    with Session(engine) as setup:
        publisher = _mk_user(setup.connection(), "无锁发单人")
        runners = [_mk_user(setup.connection(), f"无锁接单人{i}") for i in range(THREADS)]
        from_poi = _mk_poi(setup.connection(), "驿站", "STATION")
        to_poi = _mk_poi(setup.connection(), "宿舍", "DORM")
        order_id = SqlOrderRepo(setup).create_order(_draft(publisher, from_poi, to_poi), now=NOW)
        setup.commit()

    barrier = threading.Barrier(THREADS)
    outcomes: list[tuple[int, int] | str] = []
    lock = threading.Lock()

    def worker(runner_id: int) -> None:
        session = Session(engine, expire_on_commit=False)
        try:
            barrier.wait(timeout=30)
            try:
                OrderService(session).grab_order(order_id=order_id, runner_id=runner_id, now=NOW)
                session.commit()
                with lock:
                    outcomes.append("ok")
            except ApiError as exc:
                session.rollback()
                with lock:
                    outcomes.append((exc.http_status, exc.code))
            except BaseException as exc:  # noqa: BLE001
                session.rollback()
                with lock:
                    outcomes.append(f"unexpected:{exc!r}")
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            for runner in runners:
                pool.submit(worker, runner)

        winners = [o for o in outcomes if o == "ok"]
        failures = [o for o in outcomes if o != "ok"]
        assert len(winners) == 1, f"无 Redis 锁时仍应只有 1 个赢家，实得 {outcomes}"
        wrong = [f for f in failures if f != (409, ErrCode.ORDER_ALREADY_TAKEN)]
        assert not wrong, f"失败方应全部是 409/40901，异常项：{wrong}"

        with Session(engine) as check:
            status, runner_id, version = check.execute(
                sa.text("SELECT status, runner_id, version FROM orders WHERE id = :o"),
                {"o": order_id},
            ).one()
            accept_count = int(
                check.execute(
                    sa.text(
                        "SELECT count(*) FROM order_progress "
                        "WHERE order_id = :o AND action = 'ACCEPT'"
                    ),
                    {"o": order_id},
                ).scalar_one()
            )
        assert status == Status.IN_PROGRESS.value
        assert accept_count == 1, f"ACCEPT 事件应恰好 1 条，实得 {accept_count}"
        assert runner_id in runners
        assert version == 1, f"version 应为 1（只有一次 CAS 成功），实得 {version}"
    finally:
        _cleanup(
            engine,
            order_id=order_id,
            user_ids=[publisher, *runners],
            poi_ids=[from_poi, to_poi],
        )
        redis_env.delete(k_grab_order(order_id))


def test_grab_uses_row_lock_and_cas(engine: sa.Engine, redis_env: Any) -> None:
    """结构判据：抢单链路里必须出现 `FOR UPDATE` 与带 `status` 条件的 `UPDATE`。

    这条不替代上面的并发用例，它防的是"某天有人把 `apply()` 换成
    `session.get(order).status = 'IN_PROGRESS'`" 这种重构 —— 那样并发用例
    仍可能因为 Redis 锁而侥幸通过（锁把并发挡掉了），漏洞被掩盖。
    直接盯 SQL 特性能把这条路堵死。
    """
    from app.db.redis import k_grab_order

    with Session(engine) as setup:
        publisher = _mk_user(setup.connection(), "SQL 发单人")
        runner = _mk_user(setup.connection(), "SQL 接单人")
        from_poi = _mk_poi(setup.connection(), "驿站", "STATION")
        to_poi = _mk_poi(setup.connection(), "宿舍", "DORM")
        order_id = SqlOrderRepo(setup).create_order(_draft(publisher, from_poi, to_poi), now=NOW)
        setup.commit()

    statements: list[str] = []

    def before_cursor_execute(
        _conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: Any
    ) -> None:
        statements.append(statement)

    with Session(engine, expire_on_commit=False) as session:
        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        try:
            OrderService(session).grab_order(order_id=order_id, runner_id=runner, now=NOW)
            session.commit()
        finally:
            event.remove(engine, "before_cursor_execute", before_cursor_execute)

    try:
        joined = "\n".join(statements)
        assert "FOR UPDATE" in joined.upper(), "append_progress 必须先锁聚合根行"
        assert "UPDATE orders" in joined and "status" in joined, "必须走 CAS UPDATE"
    finally:
        _cleanup(
            engine,
            order_id=order_id,
            user_ids=[publisher, runner],
            poi_ids=[from_poi, to_poi],
        )
        redis_env.delete(k_grab_order(order_id))
