"""真实仓储 + Redis/Lua 的集成测试（T-05）。

## 为什么单测不够

在此之前所有状态机测试都跑在 `tests/conftest.py::MemoryOrderRepo` 上。它是**内存替身**，
有 7 处与真库语义不同（凭空建行、seq 全局自增、不校验唯一约束、不校验列名、
不做乐观锁、不模拟 CHECK、CAS 失败不回滚）。本文件逐条钉住这些差异 ——
每一条都对应一个"单测全绿、上线才炸"的坑。

## 跑法

```bash
export TEST_DATABASE_URL="postgresql+psycopg://campusrun:campusrun@127.0.0.1:5433/campusrun"
export REDIS_URL="redis://127.0.0.1:6380/0"
pytest tests -m integration
```

- 未设置 `TEST_DATABASE_URL` → 整文件 skip（默认套件不受影响）；
- 未设置/连不上 `REDIS_URL` → **只有 Lua 那组** skip，仓储用例照跑。
- 每个用例用 `SAVEPOINT` 或独立事务隔离，**不污染库**；写跨连接并发的用例自建数据、自删。

## 关于并发用例的实现

同步 SQLAlchemy + `BEGIN` 的事务里开线程不能共享 session（连接不是线程安全的），
所以并发用例用**独立连接 + 显式 DELETE 清理**，而不是回滚。这点在用例里也注明了。
"""

from __future__ import annotations

import datetime as dt
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
import sqlalchemy as sa
from app.core.clock import UTC
from app.core.errors import ApiError, ErrCode
from app.modules.order.repo import (
    OrderDraft,
    RepoProgrammingError,
    SqlOrderRepo,
    count_inflight,
    map_integrity_error,
)
from app.modules.order.state_machine import (
    Action,
    Actor,
    OrderCtx,
    ProgressEvent,
    Role,
    apply,
    guard_no_progress_45m,
    guard_silent_45m,
)
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

TEST_DATABASE_URL: str = os.environ.get("TEST_DATABASE_URL", "").strip()
REDIS_URL: str = os.environ.get("REDIS_URL", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="未设置 TEST_DATABASE_URL，跳过真实仓储集成测试",
    ),
]

NOW = dt.datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def engine() -> Iterator[sa.Engine]:
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
def db(engine: sa.Engine) -> Iterator[Session]:
    """每用例一个**外层事务**，结束回滚：不落脏数据、可重复跑。

    外层事务存在时，`append_progress` 内部的 `begin_nested()` 会发 SAVEPOINT ——
    这正是生产里"一次请求一个事务 + 局部重试"的真实形态。
    """
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            session.close()
            # 用例内部可能自己 rollback 过（模拟请求失败），此时外层事务已失效
            if transaction.is_active:
                transaction.rollback()


def _mk_user(connection: Any, name: str = "用户") -> int:
    return int(
        connection.execute(
            sa.text(
                "INSERT INTO users (nickname, credit_score, verify_status, status) "
                "VALUES (:n, 100, 'APPROVED', 'ACTIVE') RETURNING id"
            ),
            {"n": f"{name}-{uuid4().hex[:6]}"},
        ).scalar_one()
    )


def _mk_poi(connection: Any, name: str = "驿站") -> int:
    return int(
        connection.execute(
            sa.text(
                "INSERT INTO pois (campus_id, name, type, lat, lng) "
                "VALUES (1, :n, 'STATION', 32.06, 118.78) RETURNING id"
            ),
            {"n": f"{name}-{uuid4().hex[:6]}"},
        ).scalar_one()
    )


def _draft(
    publisher_id: int,
    poi_id: int,
    *,
    order_no: str | None = None,
    deadline: dt.datetime | None = None,
) -> OrderDraft:
    return OrderDraft(
        order_no=order_no or f"CRT{uuid4().hex[:10].upper()}",
        publisher_id=publisher_id,
        type="BUY",
        from_poi_id=poi_id,
        to_poi_id=poi_id,
        from_lat=Decimal("32.060000"),
        from_lng=Decimal("118.780000"),
        to_lat=Decimal("32.070000"),
        to_lng=Decimal("118.790000"),
        reward=Decimal("5.50"),
        expect_minutes=30,
        deadline=deadline or (NOW + dt.timedelta(hours=1)),
    )


def _new_order(
    db: Session,
    *,
    publisher_id: int | None = None,
    track: list[int] | None = None,
) -> tuple[int, int, int]:
    """建一个订单，返回 `(order_id, publisher_id, runner_id)`。

    `track`：把本次新建的 **user / poi id** 收集进去，供**会 commit** 的用例在
    teardown 里一并删除。回滚型用例不需要它（外层事务一 rollback 就没了），
    但并发/原子性用例必须自己提交脚手架 —— 不清理的话，每跑一次就往演示库里
    留一串 `驿站-xxxxxx` 和测试用户（实测攒到过 176 条，虽然不影响业务，
    但演示前 `GET /pois` 的计数会很难看）。
    """
    uid = publisher_id or _mk_user(db.connection(), "发单人")
    rid = _mk_user(db.connection(), "接单人")
    poi = _mk_poi(db.connection())
    if track is not None:
        track.extend([uid, rid, poi])
    order_id = SqlOrderRepo(db).create_order(_draft(uid, poi), now=NOW)
    return order_id, uid, rid


def _purge(
    engine: sa.Engine,
    *,
    order_id: int,
    tracked_ids: list[int] | None = None,
) -> None:
    """删除用例自建的脚手架：进度 → 订单 → 用户/POI（按 FK 顺序）。

    顺序不能反：`orders.publisher_id` 是 `ON DELETE RESTRICT`，
    `from_poi_id` / `to_poi_id` 也是 RESTRICT，先删订单才能删用户与 POI。
    """
    ids = list(tracked_ids or [])
    with Session(engine) as cleanup:
        cleanup.execute(
            sa.text("DELETE FROM order_progress WHERE order_id = :o"), {"o": order_id}
        )
        cleanup.execute(sa.text("DELETE FROM orders WHERE id = :o"), {"o": order_id})
        if ids:
            cleanup.execute(
                sa.text("DELETE FROM pois WHERE id = ANY(:ids)"), {"ids": ids}
            )
            cleanup.execute(
                sa.text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": ids}
            )
        cleanup.commit()


def _accept(db: Session, order_id: int, runner_id: int) -> Any:
    """走**状态机**完成 T2 抢单（`runner_id` 由 `status_patch()` 写入）。

    2025-xx 修复后不再手工 `UPDATE orders SET runner_id=...` ——
    那正是被漏掉的那个写入点，用例必须走真实路径才能守住它。

    `viewer_id=runner_id` 也是真实路径的一部分：BR-16 的在途数要按**抢单人**算，
    而此刻本单 `runner_id` 还是 NULL，不传 viewer 会算成 0。
    """
    repo = SqlOrderRepo(db)
    ctx = repo.load_ctx(order_id, viewer_id=runner_id)
    return apply(repo, ctx, Action.ACCEPT, Actor.runner(runner_id), now=NOW)


def _count(db: Session, table: str, **where: Any) -> int:
    clause = " AND ".join(f"{k} = :{k}" for k in where) or "true"
    return int(
        db.execute(sa.text(f"SELECT count(*) FROM {table} WHERE {clause}"), where).scalar_one()
    )


def _events(db: Session, order_id: int) -> list[tuple[str, int]]:
    rows = db.execute(
        sa.text("SELECT action, seq FROM order_progress WHERE order_id = :o ORDER BY seq"),
        {"o": order_id},
    ).all()
    return [(str(r[0]), int(r[1])) for r in rows]


# ===========================================================================
# 1. 未 seed 的 order_id 做 CAS：不静默成功、不建行、不落事件
# ===========================================================================
def test_cas_on_missing_order_returns_zero_and_creates_nothing(db: Session) -> None:
    """内存替身 `setdefault` 会凭空建行让 CAS"成功"；真库必须 0 行。

    这是第 1 条语义差异。真库返回 0 行 → `apply()` 抛 `409/40905`。
    """
    repo = SqlOrderRepo(db)
    ghost_id = 987_654_321

    affected = repo.cas_update_status(
        order_id=ghost_id,
        expected_status="PENDING",
        to_status="IN_PROGRESS",
        patch={},
    )
    assert affected == 0, "对不存在的订单 CAS 必须返回 0 行，不能凭空建行"
    assert _count(db, "orders", id=ghost_id) == 0
    assert _count(db, "order_progress", order_id=ghost_id) == 0


def test_apply_on_missing_order_is_business_error_not_500(db: Session) -> None:
    """伪造一个不存在的订单走 apply：必须是可识别业务错误，不能是 500 也不能静默成功。

    真库在 **append 阶段**就抛 FK（`fk_order_progress_order_id_orders`），
    比 CAS 0 行更早、指向更准确 —— 所以映射成 `404/40401`。
    内存替身做不到这一点：它会先把事件"写"进去，然后 CAS 凭空建行返回 1。
    """
    repo = SqlOrderRepo(db)
    ghost = OrderCtx(
        id=987_654_322,
        status="PENDING",
        publisher_id=1,
        runner_id=None,
        accepted_at=None,
        delivered_at=None,
        has_runner_evidence=False,
        last_runner_evidence_at=None,
        pre_dispute_status=None,
    )
    with pytest.raises(ApiError) as exc_info:
        apply(repo, ghost, Action.ACCEPT, Actor.runner(2), now=NOW)
    assert exc_info.value.code == ErrCode.ORDER_NOT_FOUND
    assert exc_info.value.http_status == 404
    assert _count(db, "orders", id=ghost.id) == 0


def test_stale_ctx_triggers_40905_and_event_is_rolled_back(engine: sa.Engine) -> None:
    """陈旧上下文（库里状态已变）→ 40905，且**事件随事务回滚不落库**。

    这是第 7 条语义差异：`apply()` 先 append 再 CAS，CAS 0 行时靠调用方事务回滚。
    内存替身没有事务，"事件已经写进去了"这个错误永远暴露不出来。

    用两条独立连接构造真实时序（ctx 读出来之后、CAS 之前状态被改），
    因此脚手架要 commit + 自己清理。
    """
    with Session(engine) as setup_db:
        tracked: list[int] = []
        order_id, _uid, rid = _new_order(setup_db, track=tracked)
        setup_db.commit()

    try:
        # ① 请求 A 读到 PENDING 的快照
        with Session(engine) as req_a:
            repo_a = SqlOrderRepo(req_a)
            stale = repo_a.load_ctx(order_id)

        # ② 请求 B 抢先把状态改掉并提交（模拟并发/另一个出口）
        with Session(engine) as req_b:
            req_b.execute(
                sa.text("UPDATE orders SET status='CANCELLED' WHERE id=:o"), {"o": order_id}
            )
            req_b.commit()

        # ③ 请求 A 用陈旧快照推进：append 成功、CAS 0 行 → 40905
        with Session(engine) as req_a:
            repo_a = SqlOrderRepo(req_a)
            with pytest.raises(ApiError) as exc_info:
                apply(repo_a, stale, Action.ACCEPT, Actor.runner(rid), now=NOW)
            assert (exc_info.value.http_status, exc_info.value.code) == (
                409,
                ErrCode.STATUS_CHANGED_CONCURRENTLY,
            )
            req_a.rollback()

        # ④ 回滚后：事件一条都不该留下，状态也还是 B 写的 CANCELLED
        with Session(engine) as check_db:
            events = _events(check_db, order_id)
            status = check_db.execute(
                sa.text("SELECT status FROM orders WHERE id=:o"), {"o": order_id}
            ).scalar_one()
        assert [a for a, _seq in events] == ["CREATE"], f"CAS 失败的事件不得落库：{events}"
        assert status == "CANCELLED"
    finally:
        _purge(engine, order_id=order_id, tracked_ids=tracked)


# ===========================================================================
# 2. seq 每单从 1 开始
# ===========================================================================
def test_seq_starts_at_one_per_order(db: Session) -> None:
    """内存替身用进程级计数器（第二单从 4 开始）；真库必须**每单从 1 开始**。"""
    repo = SqlOrderRepo(db)
    order_a, _uid, rid = _new_order(db)
    order_b, _uid2, _rid2 = _new_order(db)

    assert _events(db, order_a) == [("CREATE", 1)]
    assert _events(db, order_b) == [("CREATE", 1)], "第二单的 seq 也必须从 1 开始"

    for _ in range(2):
        repo.append_progress(
            ProgressEvent(
                order_id=order_a,
                action=Action.NOTE,
                from_status="IN_PROGRESS",
                to_status="IN_PROGRESS",
                actor_id=rid,
                actor_role=Role.RUNNER,
                note="备注",
            ),
            created_at=NOW,
        )
    assert [seq for _a, seq in _events(db, order_a)] == [1, 2, 3]
    assert [seq for _a, seq in _events(db, order_b)] == [1]


# ===========================================================================
# 3. 并发 append：20 个线程写同一单，seq 集合 = {1..20}
# ===========================================================================
def test_concurrent_append_has_no_seq_collision(engine: sa.Engine) -> None:
    """20 个并发写同一单：要么拿到不同 seq，要么重试成功；**不许 IntegrityError 逃逸**。

    内存替身没有唯一约束，这条路径在单测里根本不存在。
    这里用独立连接（每个线程一个 session），所以需要自己清理数据。
    """
    with Session(engine) as setup_db:
        tracked: list[int] = []
        order_id, _uid, rid = _new_order(setup_db, track=tracked)
        setup_db.commit()

    barrier = threading.Barrier(20)
    results: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        session = Session(engine, expire_on_commit=False)
        try:
            barrier.wait(timeout=10)
            seq = SqlOrderRepo(session).append_progress(
                ProgressEvent(
                    order_id=order_id,
                    action=Action.NOTE,
                    from_status="IN_PROGRESS",
                    to_status="IN_PROGRESS",
                    actor_id=rid,
                    actor_role=Role.RUNNER,
                    note="并发",
                ),
                created_at=NOW,
            )
            session.commit()
            with lock:
                results.append(seq)
        except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
            with lock:
                errors.append(exc)
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            for _ in range(20):
                pool.submit(worker)

        assert not errors, f"并发 append 抛异常（重试机制失效）：{[repr(e) for e in errors[:3]]}"
        # CREATE 占 seq=1，20 条并发 NOTE 应拿到 2..21
        assert sorted(results) == list(range(2, 22)), f"seq 有冲突或空洞：{sorted(results)}"
        with Session(engine) as check_db:
            seqs = [s for _a, s in _events(check_db, order_id)]
        assert seqs == list(range(1, 22)), f"库内实际 seq 序列不连续：{seqs}"
    finally:
        _purge(engine, order_id=order_id, tracked_ids=tracked)


# ===========================================================================
# 4. 两个并发 CAS：恰 1 成功、1 拿 40905
# ===========================================================================
def test_two_concurrent_cas_only_one_wins(engine: sa.Engine) -> None:
    """同一 `expected_status` 的两个 CAS：必须恰好一个成功。

    这是"抢单不能被两个人同时拿下"的最底层保证（T-20 的 Lua 只是预过滤）。
    """
    with Session(engine) as setup_db:
        tracked: list[int] = []
        order_id, _uid, rid = _new_order(setup_db, track=tracked)
        setup_db.commit()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        session = Session(engine)
        try:
            repo = SqlOrderRepo(session)
            ctx = repo.load_ctx(order_id)
            barrier.wait(timeout=10)
            try:
                apply(repo, ctx, Action.ACCEPT, Actor.runner(rid), now=NOW)
                session.commit()
                with lock:
                    outcomes.append("ok")
            except ApiError as exc:
                session.rollback()
                with lock:
                    outcomes.append(f"{exc.http_status}/{exc.code}")
        finally:
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(worker)
            pool.submit(worker)

        assert sorted(outcomes) == ["409/40905", "ok"], f"期望 1 成功 1 冲突，实得 {outcomes}"
        with Session(engine) as check_db:
            status = check_db.execute(
                sa.text("SELECT status FROM orders WHERE id = :o"), {"o": order_id}
            ).scalar_one()
            accepted = _count(check_db, "order_progress", order_id=order_id, action="ACCEPT")
        assert status == "IN_PROGRESS"
        assert accepted == 1, f"冲突的那次不得留下 ACCEPT 事件，实得 {accepted} 条"
    finally:
        _purge(engine, order_id=order_id, tracked_ids=tracked)


# ===========================================================================
# 5. version 每次成功 CAS +1
# ===========================================================================
def test_version_increments_on_each_successful_cas(db: Session) -> None:
    """`version` 是后台编辑与进度的并发校验字段，每次 CAS 成功必须 +1。"""
    repo = SqlOrderRepo(db)
    order_id, _uid, rid = _new_order(db)

    def version() -> int:
        return int(
            db.execute(sa.text("SELECT version FROM orders WHERE id = :o"), {"o": order_id}).scalar_one()
        )

    assert version() == 0
    _accept(db, order_id, rid)
    assert version() == 1
    apply(repo, repo.load_ctx(order_id), Action.PICKED_UP, Actor.runner(rid), now=NOW)
    assert version() == 2
    apply(repo, repo.load_ctx(order_id), Action.ON_THE_WAY, Actor.runner(rid), now=NOW)
    assert version() == 3


# ===========================================================================
# 6. patch 里塞不存在的列 → 编程错误
# ===========================================================================
def test_unknown_patch_key_raises_programming_error(db: Session) -> None:
    """patch 键必须是 `orders` 的真实列。这不是用户能修的错，不能映射成 4xx。"""
    repo = SqlOrderRepo(db)
    order_id, _uid, _rid = _new_order(db)
    with pytest.raises(RepoProgrammingError) as exc_info:
        repo.cas_update_status(
            order_id=order_id,
            expected_status="PENDING",
            to_status="IN_PROGRESS",
            patch={"definitely_not_a_column": 1},
        )
    assert "definitely_not_a_column" in str(exc_info.value)
    # 不是 ApiError（那会被当成业务错误返回给用户）
    assert not isinstance(exc_info.value, ApiError)
    # 状态没被动过
    assert _count(db, "orders", id=order_id, status="PENDING") == 1


# ===========================================================================
# 7. CHECK 冲突 → 业务错误码（不是 500）
# ===========================================================================
def test_check_violation_maps_to_business_code(db: Session) -> None:
    """`action='BOGUS'` 撞 CHECK 约束 → `422/42201`，而不是裸 IntegrityError/500。

    这是第 6 条语义差异：真库有一堆 CHECK，内存替身一个都没有。
    """
    repo = SqlOrderRepo(db)
    order_id, uid, _rid = _new_order(db)
    with pytest.raises(ApiError) as exc_info:
        repo.append_progress(
            ProgressEvent(
                order_id=order_id,
                action="BOGUS",  # type: ignore[arg-type] - 故意绕过枚举
                from_status="PENDING",
                to_status="PENDING",
                actor_id=uid,
                actor_role=Role.PUBLISHER,
            ),
            created_at=NOW,
        )
    assert (exc_info.value.http_status, exc_info.value.code) == (422, ErrCode.ILLEGAL_TRANSITION)


def test_duplicate_order_no_maps_to_40909(db: Session) -> None:
    """唯一冲突（`order_no` 撞号）→ `409/40909`，不是裸 IntegrityError。

    为什么单独一个用例：`create_order` 的 INSERT **不套 savepoint**（建单失败就该整单失败），
    所以一次失败的插入会把当前事务打成 aborted —— 同一个用例里不能再查库。
    """
    uid = _mk_user(db.connection())
    poi = _mk_poi(db.connection())
    repo = SqlOrderRepo(db)
    draft = _draft(uid, poi, order_no=f"CRDUP{uuid4().hex[:6].upper()}")
    repo.create_order(draft, now=NOW)

    with pytest.raises(ApiError) as exc_info:
        repo.create_order(draft, now=NOW)
    assert (exc_info.value.http_status, exc_info.value.code) == (409, ErrCode.ORDER_NO_CONFLICT)


def test_reward_out_of_range_maps_to_40003(db: Session) -> None:
    """CHECK 冲突（`reward` 越界）→ `400/40003`，不是 500。"""
    uid = _mk_user(db.connection())
    poi = _mk_poi(db.connection())
    repo = SqlOrderRepo(db)
    bad = replace(_draft(uid, poi), reward=Decimal("99.00"))

    with pytest.raises(ApiError) as exc_info:
        repo.create_order(bad, now=NOW)
    assert (exc_info.value.http_status, exc_info.value.code) == (400, ErrCode.REWARD_OUT_OF_RANGE)


def test_unmapped_integrity_error_is_reraised() -> None:
    """认不出来的 `IntegrityError` 必须**原样抛出**，不能假装是业务错误（这条不需要库）。"""
    from sqlalchemy.exc import IntegrityError

    unknown = IntegrityError("stmt", {}, Exception("something else"))
    with pytest.raises(IntegrityError):
        map_integrity_error(unknown)


# ===========================================================================
# 8. create_order 的事务性：append 失败时 orders 行也不落库
# ===========================================================================
def test_create_order_is_atomic(db: Session) -> None:
    """T1 的 `orders` 行与 `CREATE` 事件必须同生共死。

    用法：强制让 `append_progress` 抛错，回滚后查库 —— 订单行不能存在。
    内存替身做不到：它没有事务，两步之间失败会留下"有订单没有时间轴"的脏单。
    """
    uid = _mk_user(db.connection())
    poi = _mk_poi(db.connection())
    order_no = f"CRATOMIC{uuid4().hex[:6].upper()}"
    repo = SqlOrderRepo(db)

    def boom(*_args: Any, **_kwargs: Any) -> int:
        raise ApiError(500, ErrCode.INTERNAL_ERROR, "模拟 append 失败")

    # `SqlOrderRepo` 用了 `__slots__`，只能打在类上（用例内临时替换，退出即恢复）
    with patch.object(SqlOrderRepo, "append_progress", boom):
        with pytest.raises(ApiError):
            repo.create_order(_draft(uid, poi, order_no=order_no), now=NOW)

    db.rollback()
    assert _count(db, "orders", order_no=order_no) == 0, "append 失败后订单行不得落库"


# ===========================================================================
# 9. T2 必须写入 runner_id（P0 修复的验收：三条判据一次钉死）
# ===========================================================================
def test_accept_assigns_runner_id(db: Session) -> None:
    """判据 ①：抢单后 `orders.runner_id` == 接单人 id。

    修复前这里恒为 `None` —— `status_patch()` 的 `ACCEPT` 分支只写 `accepted_at`，
    全代码库没有第二个写入点。后果见下一个用例。
    """
    repo = SqlOrderRepo(db)
    order_id, _uid, rid = _new_order(db)

    before = repo.load_ctx(order_id)
    assert before.runner_id is None, "抢单前不该有接单人"

    _accept(db, order_id, rid)

    current = repo.load_ctx(order_id)
    assert current.status == "IN_PROGRESS"
    assert current.runner_id == rid, f"抢单后 runner_id 应为 {rid}，实得 {current.runner_id}"
    assert current.accepted_at == NOW
    # 两个字段是同一条 UPDATE 写的，不会一半有一半没有
    assert _count(db, "orders", id=order_id, runner_id=rid) == 1


def test_evidence_and_t16_t17_guards_after_accept(db: Session) -> None:
    """判据 ②③：接单人写完 PICKED_UP 后证据成立，且 **T16 可用 / T17 被封**。

    这是"卷件口子"的直接判据。修复前 `runner_id` 为 NULL 时，
    `load_ctx` 的 `p.actor_id = o.runner_id` 永远匹配不上，于是：
    - `has_runner_evidence` 恒 False → 接单人任何进度都不算证据；
    - **T16（失联投诉）永远不可用、T17（无责取消）永远可用** ——
      接单人明明已取件，发单人仍可零成本无责取消（RSK-11 最坏路径）。
    """
    repo = SqlOrderRepo(db)
    order_id, uid, rid = _new_order(db)
    _accept(db, order_id, rid)

    # 判据 ②：接单人推进到 PICKED_UP
    apply(repo, repo.load_ctx(order_id), Action.PICKED_UP, Actor.runner(rid), now=NOW)
    ctx = repo.load_ctx(order_id)
    assert ctx.has_runner_evidence is True, "接单人已产生进度，必须算证据"
    assert ctx.last_runner_evidence_at == NOW
    assert ctx.last_node == "PICKED_UP"

    # 判据 ③：静默 46 分钟后（>45），凭同一份**库里读出来的事实**
    # 算 T16/T17 守卫 —— 不用手搓 OrderCtx，走 load_ctx 的真实数据
    later = NOW + dt.timedelta(minutes=46)
    evidenced = repo.load_ctx(order_id)
    publisher_actor = Actor.publisher(uid)
    assert guard_silent_45m(evidenced, publisher_actor, later) is True, (
        "有进度 + 静默≥45min，T16 必须可用（否则发单人无路可走）"
    )
    assert guard_no_progress_45m(evidenced, publisher_actor, later) is False, (
        "有进度时 T17 必须被封（否则东西在接单人手里还能被无责取消）"
    )

    # 反证：同一时刻，若把它当成「无进度」（修复前的错误事实），T17 会放行 ——
    # 这正是修复前线上会发生的事
    broken = replace(evidenced, has_runner_evidence=False, last_runner_evidence_at=None)
    assert guard_no_progress_45m(broken, publisher_actor, later) is True


# ===========================================================================
# 10. load_ctx 只看 2 条 SQL 以内
# ===========================================================================
def test_load_ctx_uses_at_most_two_statements(engine: sa.Engine, db: Session) -> None:
    """`load_ctx` 必须一次拿全（N+1 的直接解法）：实测 1 条 SQL。"""
    repo = SqlOrderRepo(db)
    order_id, _uid, rid = _new_order(db)
    _accept(db, order_id, rid)
    apply(repo, repo.load_ctx(order_id), Action.PICKED_UP, Actor.runner(rid), now=NOW)

    statements: list[str] = []

    def before_cursor_execute(
        _conn: Any, _cursor: Any, statement: str, _parameters: Any, _context: Any, _many: Any
    ) -> None:
        if statement.strip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        ctx = repo.load_ctx(order_id)
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)

    assert ctx.id == order_id
    assert len(statements) <= 2, f"load_ctx 发了 {len(statements)} 条 SQL：\n" + "\n\n".join(statements)
    assert len(statements) == 1, "期望单条聚合查询"


def test_load_ctx_inflight_counts_the_viewer_not_the_order_runner(db: Session) -> None:
    """BR-16 的计数口径：算「**谁要来抢单**」的在途数，不是「本单的接单人」。

    抢单发生在 `status='PENDING'`、`runner_id IS NULL` 的时刻。初版 SQL 写的是
    `WHERE o.runner_id IS NOT NULL AND x.runner_id = o.runner_id`，第一个条件恒假，
    计数永远是 0 → `guard_grab` 的 `inflight < 3` 永远放行 →
    **手上已有 3 单在途的接单人还能抢第 4 单**（engineer-2 的集成用例实际撞到）。

    修法：`load_ctx(viewer_id=...)`；不传时回落到本单接单人（旧语义不变）。
    """
    repo = SqlOrderRepo(db)
    busy_runner = _mk_user(db.connection(), "在途3单的接单人")
    idle_runner = _mk_user(db.connection(), "空闲接单人")

    # ① 让 busy_runner 手上真的有 3 单 IN_PROGRESS
    for index in range(3):
        publisher = _mk_user(db.connection(), f"发单人{index}")
        poi = _mk_poi(db.connection())
        order_id = repo.create_order(_draft(publisher, poi), now=NOW)
        _accept(db, order_id, busy_runner)

    assert count_inflight(db, busy_runner) == 3

    # ② 再造一个**待接单**的单（正是抢单发生的那一刻）
    publisher = _mk_user(db.connection(), "待接单的发单人")
    poi = _mk_poi(db.connection())
    pending_id = repo.create_order(_draft(publisher, poi), now=NOW)

    # 传 viewer：按抢单人算 → 3（守卫 3 < 3 为假 → 正确拒绝第 4 单）
    assert repo.load_ctx(pending_id, viewer_id=busy_runner).runner_inflight_count == 3
    # 空闲接单人 → 0
    assert repo.load_ctx(pending_id, viewer_id=idle_runner).runner_inflight_count == 0
    # 不传 viewer：回落到本单接单人（PENDING 时是 NULL）→ 0，旧语义保持不变
    assert repo.load_ctx(pending_id).runner_inflight_count == 0
    # 已接单后，不传 viewer 也能拿到本单接单人的在途数
    assert repo.load_ctx(order_id).runner_inflight_count == 3


def test_viewer_id_does_not_leak_into_other_derived_facts(db: Session) -> None:
    """`viewer_id` 只影响在途数，不能顺手改变别的派生事实（防"顺手多算一个"）。"""
    repo = SqlOrderRepo(db)
    order_id, uid, rid = _new_order(db)
    _accept(db, order_id, rid)

    without = repo.load_ctx(order_id)
    with_viewer = repo.load_ctx(order_id, viewer_id=rid)
    assert with_viewer.runner_inflight_count == without.runner_inflight_count == 1
    for field in ("status", "runner_id", "publisher_id", "dispute_count", "urge_count",
                  "last_node", "has_runner_evidence", "publisher_pending_count"):
        assert getattr(without, field) == getattr(with_viewer, field), (
            f"{field} 不该受 viewer_id 影响"
        )
    assert uid == with_viewer.publisher_id


# ===========================================================================
# 11. 原子性：status 与本行字段必须在**同一条 UPDATE** 里
# ===========================================================================
def _count_updates_to_orders(engine: sa.Engine, action: Any) -> tuple[int, list[str]]:
    """执行 `action()` 期间统计发往 `orders` 的 UPDATE 条数。"""
    statements: list[str] = []

    def before_cursor_execute(
        _conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: Any
    ) -> None:
        head = statement.lstrip().upper()
        if head.startswith("UPDATE") and "ORDERS" in head:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    return len(statements), statements


def test_dispute_open_writes_status_and_settle_state_atomically(
    engine: sa.Engine, db: Session
) -> None:
    """`status='DISPUTED'` 与 `settle_state='FROZEN'` 必须**一条 UPDATE** 同时写。

    这不是"最终值对就行"：`freeze` 曾经被声明成"事务提交后派发的副作用"，
    若照字面实现，两次事务之间崩一次就会留下 `DISPUTED` 但 `UNSETTLED` 的单 ——
    状态对了、冻结没了，而且**不报错**（可绕过冻结逻辑继续操作）。
    """
    repo = SqlOrderRepo(db)
    order_id, uid, rid = _new_order(db)
    _accept(db, order_id, rid)
    for action in (Action.PICKED_UP, Action.ON_THE_WAY, Action.DELIVER):
        apply(repo, repo.load_ctx(order_id), action, Actor.runner(rid), now=NOW)

    update_count, statements = _count_updates_to_orders(
        engine,
        lambda: apply(repo, repo.load_ctx(order_id), Action.DISPUTE_OPENED,
                      Actor.publisher(uid), now=NOW),
    )
    assert update_count == 1, f"status 与 settle_state 必须同一条 UPDATE，实得 {update_count} 条：\n" + "\n".join(statements)

    # 一条 SELECT 同时读出两列 —— 它们必然同时成立，不存在中间态
    status, settle_state, pre = db.execute(
        sa.text("SELECT status, settle_state, pre_dispute_status FROM orders WHERE id = :o"),
        {"o": order_id},
    ).one()
    assert (status, settle_state) == ("DISPUTED", "FROZEN")
    assert pre == "DELIVERED", "挂起态也要同一条 UPDATE 写（BR-35）"


def test_arbitrate_unfreezes_atomically(engine: sa.Engine, db: Session) -> None:
    """出 DISPUTED（T14）时 `status`/`settle_state`/`pre_dispute_status` 也一条 UPDATE 写完。"""
    repo = SqlOrderRepo(db)
    order_id, uid, rid = _new_order(db)
    _accept(db, order_id, rid)
    for action in (Action.PICKED_UP, Action.ON_THE_WAY, Action.DELIVER):
        apply(repo, repo.load_ctx(order_id), action, Actor.runner(rid), now=NOW)
    apply(repo, repo.load_ctx(order_id), Action.DISPUTE_OPENED, Actor.publisher(uid), now=NOW)

    update_count, _ = _count_updates_to_orders(
        engine,
        lambda: apply(repo, repo.load_ctx(order_id), Action.ARBITRATE_FREE,
                      Actor.admin(999), now=NOW),
    )
    assert update_count == 1

    status, settle_state, pre = db.execute(
        sa.text("SELECT status, settle_state, pre_dispute_status FROM orders WHERE id = :o"),
        {"o": order_id},
    ).one()
    assert (status, settle_state, pre) == ("DELIVERED", "UNSETTLED", None)


@pytest.mark.parametrize(
    "deliver_offset,expected_late",
    [
        (dt.timedelta(hours=-1), False),   # 送达早于 deadline → 不超时
        (dt.timedelta(hours=1), True),     # 送达晚于 deadline → 超时
    ],
    ids=["before_deadline", "after_deadline"],
)
def test_deliver_marks_late_by_deadline(
    db: Session, deliver_offset: dt.timedelta, expected_late: bool
) -> None:
    """BR-23：`is_late = delivered_at > deadline`，两个方向都验。

    必须用注入的 `now`（= delivered_at）与 `ctx.deadline` 比，
    **不能读 DB 的 `now()`** —— 否则测试无法注入时间，超时率永远测不准。

    `after_deadline` 场景的真实语义是「抢单时未过期、送达时已过期」：
    deadline 固定 `NOW + 2h`（抢单时刻 NOW 早于它，能抢）；送达时刻
    `NOW + 2h + deliver_offset`。after_deadline 时送达晚于 deadline → 超时。
    不能用「建单即把 deadline 设到过去」——那样抢单守卫会先拒绝，根本走不到 DELIVER。
    """
    uid = _mk_user(db.connection())
    rid = _mk_user(db.connection(), "接单人")
    poi = _mk_poi(db.connection())
    repo = SqlOrderRepo(db)
    deadline = NOW + dt.timedelta(hours=2)
    order_id = repo.create_order(_draft(uid, poi, deadline=deadline), now=NOW)
    _accept(db, order_id, rid)
    for action in (Action.PICKED_UP, Action.ON_THE_WAY):
        apply(repo, repo.load_ctx(order_id), action, Actor.runner(rid), now=NOW)

    deliver_now = NOW + dt.timedelta(hours=2) + deliver_offset
    result = apply(repo, repo.load_ctx(order_id), Action.DELIVER, Actor.runner(rid), now=deliver_now)
    assert result.status_patch["is_late"] is expected_late

    is_late = db.execute(
        sa.text("SELECT is_late FROM orders WHERE id = :o"), {"o": order_id}
    ).scalar_one()
    assert is_late is expected_late


def test_atomicity_assertions_are_not_tautological(
    db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**反证**：把 `settle_state` 从 patch 里拿掉，上面那条断言必须失效。

    否则那些用例只是"最终值恰好对"，并没有真正守住"同一条 UPDATE"。
    这里人为制造"只写 status 不写 settle_state"的世界，观察它确实会留下不一致的单。
    """
    from app.modules.order import state_machine as sm

    repo = SqlOrderRepo(db)
    order_id, uid, rid = _new_order(db)
    _accept(db, order_id, rid)
    for action in (Action.PICKED_UP, Action.ON_THE_WAY, Action.DELIVER):
        apply(repo, repo.load_ctx(order_id), action, Actor.runner(rid), now=NOW)

    original = sm.status_patch

    def stripped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        patch = original(*args, **kwargs)
        patch.pop("settle_state", None)      # 模拟"冻结没进 in-tx"
        return patch

    monkeypatch.setattr(sm, "status_patch", stripped)
    apply(repo, repo.load_ctx(order_id), Action.DISPUTE_OPENED, Actor.publisher(uid), now=NOW)
    monkeypatch.undo()

    status, settle_state = db.execute(
        sa.text("SELECT status, settle_state FROM orders WHERE id = :o"), {"o": order_id}
    ).one()
    # 这就是 bug 的样子：状态改了、冻结没了，且没有任何报错
    assert (status, settle_state) == ("DISPUTED", "UNSETTLED")
    assert (status, settle_state) != ("DISPUTED", "FROZEN")


def test_settle_state_frozen_iff_disputed(db: Session) -> None:
    """不变量：`settle_state='FROZEN'` 当且仅当 `status='DISPUTED'`。

    覆盖所有"进出 DISPUTED"的路径（T11/T12/T16 进；T13/T14/T15 与从 DISPUTED 的
    FORCE_CLOSE 出），任何一条漏写都会破坏它。
    """
    repo = SqlOrderRepo(db)
    order_id, uid, rid = _new_order(db)
    _accept(db, order_id, rid)
    for action in (Action.PICKED_UP, Action.ON_THE_WAY, Action.DELIVER):
        apply(repo, repo.load_ctx(order_id), action, Actor.runner(rid), now=NOW)

    def settle() -> str:
        return str(
            db.execute(
                sa.text("SELECT settle_state FROM orders WHERE id = :o"), {"o": order_id}
            ).scalar_one()
        )

    assert settle() == "UNSETTLED"
    apply(repo, repo.load_ctx(order_id), Action.DISPUTE_OPENED, Actor.publisher(uid), now=NOW)
    assert settle() == "FROZEN"
    apply(repo, repo.load_ctx(order_id), Action.ARBITRATE_LIABLE, Actor.admin(999), now=NOW)
    assert settle() == "UNSETTLED", "T15 担责也必须解冻"


# ===========================================================================
# 12. Lua 装载与执行（需要 Redis）
# ===========================================================================
@pytest.fixture(scope="module")
def redis_client() -> Iterator[Any]:
    if not REDIS_URL:
        pytest.skip("未设置 REDIS_URL，跳过 Lua 用例")
    import redis as redis_lib

    client = redis_lib.Redis.from_url(REDIS_URL, decode_responses=False, socket_connect_timeout=2)
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis 不可达（{REDIS_URL}）：{exc}")
    yield client
    client.close()


def test_lua_scripts_load_and_execute(redis_client: Any) -> None:
    """三个脚本必须能 `SCRIPT LOAD` 并真的执行（占位语义可验证）。"""
    from app.db.redis import (
        LUA_SCRIPTS,
        bump_quota,
        grab_lock,
        heartbeat,
        k_alive,
        k_geo_runners,
        k_grab_order,
        k_push_quota,
        load_all_scripts,
    )

    shas = load_all_scripts(redis_client)
    assert set(shas) == {"grab", "quota", "presence"}
    assert all(len(sha) == 40 for sha in shas.values()), f"sha 长度异常：{shas}"

    order_id = 910_001
    key = k_grab_order(order_id)
    try:
        assert grab_lock(redis_client, order_id=order_id, token="tok-a", ttl_ms=5000) is True
        assert grab_lock(redis_client, order_id=order_id, token="tok-b", ttl_ms=5000) is False
        assert redis_client.exists(key) == 1
        assert 0 < redis_client.pttl(key) <= 5000
    finally:
        redis_client.delete(key)

    quota_key = k_push_quota(920_001, "10m")
    try:
        assert [bump_quota(redis_client, user_id=920_001) for _ in range(3)] == [1, 2, 3]
        assert redis_client.ttl(quota_key) > 0, "首次 INCR 必须同时设上 TTL"
    finally:
        redis_client.delete(quota_key)

    geo_key = k_geo_runners(1)
    alive_key = k_alive(930_001)
    try:
        assert heartbeat(redis_client, campus_id=1, user_id=930_001, lng=118.78, lat=32.06) is True
        assert redis_client.zscore(geo_key, 930_001) is not None
        assert redis_client.exists(alive_key) == 1
        assert redis_client.ttl(alive_key) > 0
    finally:
        redis_client.zrem(geo_key, 930_001)
        redis_client.delete(alive_key)

    assert {s.name for s in LUA_SCRIPTS.values()} == {"grab", "quota", "presence"}


def test_lua_recovers_from_script_flush(redis_client: Any) -> None:
    """`SCRIPT FLUSH` 后必须靠 `NOSCRIPT` 自动重载，否则线上表现为"Redis 一重启全 500"。"""
    from app.db.redis import grab_lock, grab_script, k_grab_order

    grab_script.load(redis_client)
    stale_sha = grab_script.sha
    assert stale_sha

    redis_client.script_flush()          # 模拟 Redis 重启 / 主从切换
    order_id = 910_002
    # 清理必须用**键名规范的构造函数**：`grab_lock` 写的是 `grab:order:{id}`，
    # 曾经这里手写成 `f"test:grab:{id}"`（一个从来不存在的键），于是锁一泄漏就得
    # 靠 3s TTL 兜底 —— 两次连续跑测试时，第二次会在 `SET NX` 上拿到 False 而
    # 假失败（不是脚本重载失败，是上一轮的锁还在）。
    key = k_grab_order(order_id)
    try:
        # 缓存里的 sha 已失效 → 内部应捕获 NOSCRIPT、重载、重试
        assert grab_lock(redis_client, order_id=order_id, token="tok", ttl_ms=5000) is True
    finally:
        redis_client.delete(key)
    assert grab_script.sha, "重载后应重新缓存 sha"
    assert redis_client.script_exists(grab_script.sha)[0] is True
