"""抢单 / 履约 / 确认收货的**集成**测试（T-20，真 PG + 真 Redis）。

## 为什么这些用例必须跑真库

抢单链路的正确性有一半不在 Python 里：
- **CAS**（`UPDATE orders ... WHERE status='PENDING'`）—— 内存替身会"凭空建行"，永远成功；
- **`runner_id` 的写入点**（`status_patch` 的 `ACCEPT` 分支）—— 单测里 `OrderCtx.runner_id`
  是手填的，看不出"抢完单接单人还是 NULL"这个 P0（测试库里踩过一次）；
- **CHECK / 唯一约束**（`order_progress.action` 枚举、`UNIQUE(order_id, seq)`）；
- **Redis 锁的释放**（`grab_unlock` 校验 token）—— 只有真 Redis 才验证得到。

## 隔离方式（与 `test_integration_repo.py` 一致）

每个用例开一个**外层事务**，结束时 `rollback`：不落脏数据、可重复跑。
`append_progress` 内部的 `begin_nested()` 在外层事务存在时自动退化为 SAVEPOINT ——
这正是生产里"一次请求一个事务 + 局部重试"的真实形态。

Redis 侧用独立键空间（`grab:order:{id}`），用例结束显式删除；同时每个用例
注入一个干净的 `Redis` 客户端，避免单例跨用例串味。

## 跑法

```bash
export TEST_DATABASE_URL="postgresql+psycopg://campusrun:campusrun@127.0.0.1:5433/campusrun"
export REDIS_URL="redis://127.0.0.1:6380/0"
pytest tests -m integration
```
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from app.core.clock import UTC
from app.core.errors import ApiError, ErrCode
from app.modules.order.queries import HALL_LIMIT
from app.modules.order.repo import OrderDraft, SqlOrderRepo
from app.modules.order.service import PROGRESS_ACTIONS, OrderService
from app.modules.order.state_machine import Action, Status
from sqlalchemy.orm import Session

TEST_DATABASE_URL: str = os.environ.get("TEST_DATABASE_URL", "").strip()
REDIS_URL: str = os.environ.get("REDIS_URL", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="未设置 TEST_DATABASE_URL，跳过真实仓储集成测试",
    ),
]

#: UTC 02:00 = 北京 10:00（BR-10 发单窗内，且不贴窗边）
NOW: dt.datetime = dt.datetime(2025, 6, 1, 2, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def engine() -> Iterator[sa.Engine]:
    if not TEST_DATABASE_URL:
        pytest.skip("未设置 TEST_DATABASE_URL")
    from sqlalchemy.pool import NullPool

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
    """每用例一个外层事务，结束回滚（不污染库）。"""
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            session.close()
            if transaction.is_active:
                transaction.rollback()


@pytest.fixture(scope="module")
def redis_client() -> Iterator[Any]:
    if not REDIS_URL:
        pytest.skip("未设置 REDIS_URL，跳过需要 Redis 的用例")
    import redis as redis_lib

    client = redis_lib.Redis.from_url(
        REDIS_URL, decode_responses=False, socket_connect_timeout=2
    )
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis 不可达（{REDIS_URL}）：{exc}")
    yield client
    client.close()


@pytest.fixture()
def redis_env(redis_client: Any) -> Iterator[Any]:
    """把 `app.db.redis` 的进程级单例指向测试客户端，用例后重置。"""
    from app.db import redis as redis_mod

    redis_mod.set_redis(redis_client)
    try:
        yield redis_client
    finally:
        redis_mod.reset_for_tests()


@pytest.fixture(autouse=True)
def _purge_grab_locks(redis_client: Any) -> Iterator[None]:
    """每个用例**前后**清掉 `grab:*` 锁键。

    ## 为什么需要（实测过的一次假失败）

    `grab:order:{oid}` 这把锁的键只由订单 id 决定，而订单 id 来自**共享的序列**。
    两个 pytest 进程（或同一个人连跑两次、且上一轮的锁还没到 3s TTL）撞上同一个
    `oid` 时，后来者会拿到一个"还带着别人 token 的锁"，`grab_lock` 直接返回 False
    → 接口抛 `409/40901`（"手慢了"），于是一条本该断言 `40903` 的用例偶发变红。

    那不是被测代码的问题，是**测试之间共享了进程外状态**却没人清理。
    锁的 TTL 只有 3s，理论上会自愈 —— 但"理论上会自愈"正是 flaky 的来源：
    跑得快就红、喘口气就绿。

    ## 为什么可以整段前缀清掉

    本文件的用例都是**串行**执行的（pytest 默认单线程），而这个文件里的锁
    生命周期都包在单个请求里（`grab_order` 的 `finally` 会自己 release）。
    也就是说：用例结束后，`grab:*` 下任何还在的键都属于"上一轮/上一个进程的残留"，
    删掉不会影响任何活跃请求。仓库里唯一并发的用例在
    `test_grab_concurrent.py`，它用自己的 clean-up，且**不依赖**本 fixture。
    """
    def _purge() -> None:
        for key in redis_client.scan_iter("grab:*"):
            redis_client.delete(key)

    _purge()
    try:
        yield
    finally:
        _purge()


# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------
def _mk_user(connection: Any, name: str = "用户", *, verify: str = "APPROVED") -> int:
    return int(
        connection.execute(
            sa.text(
                "INSERT INTO users (nickname, credit_score, verify_status, status) "
                "VALUES (:n, 100, :v, 'ACTIVE') RETURNING id"
            ),
            {"n": f"{name}-{uuid4().hex[:6]}", "v": verify},
        ).scalar_one()
    )


def _mk_poi(connection: Any, name: str = "驿站", poi_type: str = "STATION") -> int:
    return int(
        connection.execute(
            sa.text(
                "INSERT INTO pois (campus_id, name, type, lat, lng) "
                "VALUES (1, :n, :t, 32.057200, 118.639800) RETURNING id"
            ),
            {"n": f"{name}-{uuid4().hex[:6]}", "t": poi_type},
        ).scalar_one()
    )


def _draft(
    publisher_id: int,
    from_poi: int,
    to_poi: int,
    *,
    order_type: str = "BUY",
    pick_code_enc: bytes | None = None,
    deadline: dt.datetime | None = None,
) -> OrderDraft:
    return OrderDraft(
        order_no=f"CRT{uuid4().hex[:10].upper()}",
        publisher_id=publisher_id,
        type=order_type,
        from_poi_id=from_poi,
        to_poi_id=to_poi,
        from_lat=Decimal("32.057200"),
        from_lng=Decimal("118.639800"),
        to_lat=Decimal("32.058100"),
        to_lng=Decimal("118.641800"),
        reward=Decimal("5.50"),
        expect_minutes=60,
        deadline=deadline or (NOW + dt.timedelta(hours=1)),
        description="集成测试",
        pick_code_enc=pick_code_enc,
    )


def _new_order(
    db: Session, *, publisher_id: int | None = None
) -> tuple[int, int, int]:
    """建一个 PENDING 订单，返回 `(order_id, publisher_id, runner_id)`。"""
    publisher = publisher_id or _mk_user(db.connection(), "发单人")
    runner = _mk_user(db.connection(), "接单人")
    from_poi = _mk_poi(db.connection(), "驿站", "STATION")
    to_poi = _mk_poi(db.connection(), "宿舍", "DORM")
    order_id = SqlOrderRepo(db).create_order(_draft(publisher, from_poi, to_poi), now=NOW)
    return order_id, publisher, runner


def _order_row(db: Session, order_id: int) -> dict[str, Any]:
    row = db.execute(
        sa.text(
            "SELECT status, runner_id, accepted_at, delivered_at, completed_at, "
            "       auto_confirm_due_at, is_late, version "
            "FROM orders WHERE id = :o"
        ),
        {"o": order_id},
    ).mappings().one()
    return dict(row)


def _events(db: Session, order_id: int) -> list[str]:
    rows = db.execute(
        sa.text("SELECT action FROM order_progress WHERE order_id = :o ORDER BY seq"),
        {"o": order_id},
    ).all()
    return [str(row[0]) for row in rows]


def _accept(db: Session, order_id: int, runner_id: int) -> None:
    """走状态机完成 T2（复核 `runner_id` 由 `status_patch()` 写入）。"""
    repo = SqlOrderRepo(db)
    from app.modules.order.state_machine import Actor, apply

    apply(repo, repo.load_ctx(order_id), Action.ACCEPT, Actor.runner(runner_id), now=NOW)


# ===========================================================================
# 1. 抢单正例：状态、runner_id、accepted_at、事件、Redis 锁释放
# ===========================================================================
def test_grab_happy_path(db: Session, redis_env: Any) -> None:
    """T2 一次成功：`PENDING → IN_PROGRESS`，写入 `runner_id` 与 `accepted_at`。

    这四条断言对应四个曾经各自出过问题的点：
    ① 状态真的变了（CAS 生效）；
    ② `runner_id` 不是 NULL（**曾经全代码库没有写入点**，抢完单接单人还是空）；
    ③ `accepted_at` 与 `runner_id` 同一条 UPDATE 写入（不出现"一半有一半没有"）；
    ④ `order_progress` 里恰好一条 `ACCEPT`（append-only 事实流是投影对账的基准）。
    """
    order_id, _publisher, runner = _new_order(db)
    service = OrderService(db)

    result = service.grab_order(order_id=order_id, runner_id=runner, now=NOW)

    assert result.transition_id == "T2"
    assert result.to_status == Status.IN_PROGRESS.value
    row = _order_row(db, order_id)
    assert row["status"] == Status.IN_PROGRESS.value
    assert row["runner_id"] == runner
    assert row["accepted_at"] == NOW
    assert row["version"] == 1, "每次成功 CAS 必须 version+1"
    assert _events(db, order_id) == ["CREATE", "ACCEPT"]


def test_grab_releases_redis_lock(db: Session, redis_env: Any) -> None:
    """抢单成功后必须**释放** Redis 锁，否则该单 3 秒内无人能再操作。

    释放必须校验 token（`grab_unlock`）：直接 `DEL` 会误删别人已拿到的锁，
    那才是"锁失效"的真因（TTL 只是兜底）。
    """
    from app.db.redis import k_grab_order

    order_id, _publisher, runner = _new_order(db)
    OrderService(db).grab_order(order_id=order_id, runner_id=runner, now=NOW)
    assert redis_env.exists(k_grab_order(order_id)) == 0, "抢单结束后锁必须已被释放"


def test_grab_unlock_checks_token(redis_env: Any) -> None:
    """`grab_unlock` 只删自己那把锁：token 不匹配时必须什么都不做。"""
    from app.db.redis import grab_lock, grab_unlock, k_grab_order

    order_id = 970_001
    key = k_grab_order(order_id)
    try:
        assert grab_lock(redis_env, order_id=order_id, token="owner", ttl_ms=5000) is True
        assert grab_unlock(redis_env, order_id=order_id, token="intruder") is False
        assert redis_env.exists(key) == 1, "别人的锁不能被删"
        assert grab_unlock(redis_env, order_id=order_id, token="owner") is True
        assert redis_env.exists(key) == 0
    finally:
        redis_env.delete(key)


# ===========================================================================
# 2. 抢单四种失败各回各的码（6.3）
# ===========================================================================
def test_grab_own_order_is_40303(db: Session, redis_env: Any) -> None:
    """不能接自己发的单 → `403/40303`（不是笼统的 409）。"""
    order_id, publisher, _runner = _new_order(db)
    with pytest.raises(ApiError) as exc_info:
        OrderService(db).grab_order(order_id=order_id, runner_id=publisher, now=NOW)
    assert (exc_info.value.http_status, exc_info.value.code) == (403, ErrCode.CANNOT_GRAB_OWN_ORDER)
    assert _order_row(db, order_id)["status"] == Status.PENDING.value


def test_grab_taken_order_is_40901(db: Session, redis_env: Any) -> None:
    """已被接走的单再抢 → `409/40901`（"手慢了"）。

    判据是 `runner_id` 是否为空，不是状态值本身：并发抢单时 19 个失败者看到的
    库内状态可能是 `PENDING`（赢家还没提交）也可能是 `IN_PROGRESS`（已提交），
    但对他们而言是同一件事 —— "这单有人了"。所以两种情况必须回同一个码，
    否则同一次抢单会产出两种错误码，前端得写两套文案。
    """
    order_id, _publisher, runner = _new_order(db)
    service = OrderService(db)
    service.grab_order(order_id=order_id, runner_id=runner, now=NOW)

    other = _mk_user(db.connection(), "接单人2")
    with pytest.raises(ApiError) as exc_info:
        service.grab_order(order_id=order_id, runner_id=other, now=NOW)
    assert (exc_info.value.http_status, exc_info.value.code) == (
        409,
        ErrCode.ORDER_ALREADY_TAKEN,
    )
    assert _order_row(db, order_id)["runner_id"] == runner, "不得覆盖第一个接单人"


def test_grab_closed_order_is_40903(db: Session, redis_env: Any) -> None:
    """**没人接过**但已关闭（T3 取消 → `CANCELLED`）→ `409/40903`。

    与上一条的区别只在"有没有人接过"：`runner_id IS NULL` 时用户该做的是
    "刷新列表"，不是"换一单再试"。
    """
    from app.modules.order.state_machine import Actor, apply

    order_id, publisher, runner = _new_order(db)
    repo = SqlOrderRepo(db)
    apply(repo, repo.load_ctx(order_id), Action.CANCEL, Actor.publisher(publisher), now=NOW)

    with pytest.raises(ApiError) as exc_info:
        OrderService(db).grab_order(order_id=order_id, runner_id=runner, now=NOW)
    assert (exc_info.value.http_status, exc_info.value.code) == (
        409,
        ErrCode.ORDER_STATUS_NOT_GRABBABLE,
    )
    assert _order_row(db, order_id)["status"] == Status.CANCELLED.value


def test_grab_inflight_limit_is_40902(db: Session, redis_env: Any) -> None:
    """在途已满 3 单 → `409/40902`（BR-16），且第 4 单保持 PENDING。"""
    service = OrderService(db)
    runner = _mk_user(db.connection(), "忙碌接单人")
    for _ in range(3):
        order_id, _publisher, _ = _new_order(db)
        service.grab_order(order_id=order_id, runner_id=runner, now=NOW)

    target, _publisher, _ = _new_order(db)
    with pytest.raises(ApiError) as exc_info:
        service.grab_order(order_id=target, runner_id=runner, now=NOW)
    assert (exc_info.value.http_status, exc_info.value.code) == (
        409,
        ErrCode.INFLIGHT_LIMIT_REACHED,
    )
    assert _order_row(db, target)["status"] == Status.PENDING.value


def test_grab_unverified_is_40301(db: Session, redis_env: Any) -> None:
    """未认证用户抢单 → `403/40301`（`guard_grab` 的 `is_verified` 硬条件）。

    `is_verified` 由 JWT 背后的 `users.verify_status` 决定（`deps.CurrentUser`），
    不是客户端声明 —— 所以这里显式传 `is_verified=False` 模拟未认证账号。
    """
    order_id, _publisher, _runner = _new_order(db)
    unverified = _mk_user(db.connection(), "未认证", verify="UNVERIFIED")
    with pytest.raises(ApiError) as exc_info:
        OrderService(db).grab_order(
            order_id=order_id, runner_id=unverified, is_verified=False, now=NOW
        )
    assert (exc_info.value.http_status, exc_info.value.code) == (403, ErrCode.NOT_VERIFIED)
    assert _order_row(db, order_id)["runner_id"] is None


def test_grab_missing_order_is_404(db: Session, redis_env: Any) -> None:
    """不存在的订单 → `404/40401`，且**不**占用 Redis 锁。"""
    from app.db.redis import k_grab_order

    ghost = 987_654_321
    with pytest.raises(ApiError) as exc_info:
        OrderService(db).grab_order(order_id=ghost, runner_id=1, now=NOW)
    assert exc_info.value.code == ErrCode.ORDER_NOT_FOUND
    assert redis_env.exists(k_grab_order(ghost)) == 0


# ===========================================================================
# 3. 履约：节点顺序、超时标记、24h 自动确认时刻
# ===========================================================================
def test_progress_requires_sequential_nodes(db: Session, redis_env: Any) -> None:
    """`PICKED_UP → ON_THE_WAY → DELIVER` 严格顺序；跳级一律 `42201`。

    跳级放行的后果不是"少一条日志"：`PICKED_UP` 是接单人自证"东西在我手上"的
    唯一节点，也是 T16/T17 判 `has_runner_evidence` 的基础。少它 = 证据链断了。
    """
    order_id, _publisher, runner = _new_order(db)
    service = OrderService(db)
    service.grab_order(order_id=order_id, runner_id=runner, now=NOW)

    with pytest.raises(ApiError) as exc_info:
        service.advance_progress(
            order_id=order_id, user_id=runner, action=Action.DELIVER, now=NOW
        )
    assert (exc_info.value.http_status, exc_info.value.code) == (422, ErrCode.ILLEGAL_TRANSITION)
    assert _order_row(db, order_id)["status"] == Status.IN_PROGRESS.value

    service.advance_progress(order_id=order_id, user_id=runner, action=Action.PICKED_UP, now=NOW)
    service.advance_progress(order_id=order_id, user_id=runner, action=Action.ON_THE_WAY, now=NOW)
    result = service.advance_progress(
        order_id=order_id, user_id=runner, action=Action.DELIVER, now=NOW
    )

    assert result.to_status == Status.DELIVERED.value
    row = _order_row(db, order_id)
    assert row["delivered_at"] == NOW
    assert row["is_late"] is False, "NOW 早于 deadline，不应判超时"
    assert row["auto_confirm_due_at"] == NOW + dt.timedelta(hours=24)
    assert _events(db, order_id) == [
        "CREATE",
        "ACCEPT",
        "PICKED_UP",
        "ON_THE_WAY",
        "DELIVER",
    ]


def test_progress_by_publisher_is_rejected(db: Session, redis_env: Any) -> None:
    """发单人不能推进接单人的节点（`42201`：角色不匹配的守卫失败）。"""
    order_id, publisher, runner = _new_order(db)
    service = OrderService(db)
    service.grab_order(order_id=order_id, runner_id=runner, now=NOW)

    with pytest.raises(ApiError) as exc_info:
        service.advance_progress(
            order_id=order_id, user_id=publisher, action=Action.PICKED_UP, now=NOW
        )
    assert exc_info.value.code in {
        ErrCode.ILLEGAL_TRANSITION,
        ErrCode.ORDER_NOT_YOURS,
    }
    assert _events(db, order_id) == ["CREATE", "ACCEPT"]


def test_progress_whitelist_rejects_other_actions(db: Session, redis_env: Any) -> None:
    """`advance_progress` 只接受三个节点（+`NOTE`）：`CONFIRM` 必须被拒。

    为什么在这里拦：`CONFIRM` 的合法入口是 `/confirm`（发单人），
    如果 `/progress` 也放行，就等于把"确认收货"的权限口径开了两个洞。
    """
    order_id, _publisher, runner = _new_order(db)
    service = OrderService(db)
    service.grab_order(order_id=order_id, runner_id=runner, now=NOW)

    assert Action.CONFIRM not in PROGRESS_ACTIONS
    with pytest.raises(ApiError) as exc_info:
        service.advance_progress(
            order_id=order_id, user_id=runner, action=Action.CONFIRM, now=NOW
        )
    assert (exc_info.value.http_status, exc_info.value.code) == (422, ErrCode.ILLEGAL_TRANSITION)


def test_deliver_after_deadline_marks_late(db: Session, redis_env: Any) -> None:
    """BR-23：`is_late = delivered_at > deadline`（用注入时刻比，不读 DB `now()`）。"""
    order_id, _publisher, runner = _new_order(db)
    service = OrderService(db)
    service.grab_order(order_id=order_id, runner_id=runner, now=NOW)
    service.advance_progress(order_id=order_id, user_id=runner, action=Action.PICKED_UP, now=NOW)
    service.advance_progress(order_id=order_id, user_id=runner, action=Action.ON_THE_WAY, now=NOW)

    late_moment = NOW + dt.timedelta(hours=2)      # deadline = NOW + 1h
    service.advance_progress(
        order_id=order_id, user_id=runner, action=Action.DELIVER, now=late_moment
    )
    row = _order_row(db, order_id)
    assert row["is_late"] is True
    assert row["auto_confirm_due_at"] == late_moment + dt.timedelta(hours=24), (
        "24h 自动确认必须从**送达时刻**起算"
    )


# ===========================================================================
# 4. 确认收货（T9）
# ===========================================================================
def test_confirm_only_by_publisher_and_only_after_delivered(db: Session, redis_env: Any) -> None:
    """T9：只有发单人能确认，且必须已 `DELIVERED`。"""
    order_id, publisher, runner = _new_order(db)
    service = OrderService(db)
    service.grab_order(order_id=order_id, runner_id=runner, now=NOW)

    # ① 未送达就确认 → 42201
    with pytest.raises(ApiError) as exc_info:
        service.confirm_receipt(order_id=order_id, user_id=publisher, now=NOW)
    assert (exc_info.value.http_status, exc_info.value.code) == (422, ErrCode.ILLEGAL_TRANSITION)

    for action in (Action.PICKED_UP, Action.ON_THE_WAY, Action.DELIVER):
        service.advance_progress(order_id=order_id, user_id=runner, action=action, now=NOW)

    # ② 接单人确认 → 403/40310（不是该单的相关方里的"发单人"角色）
    with pytest.raises(ApiError) as exc_info:
        service.confirm_receipt(order_id=order_id, user_id=runner, now=NOW)
    assert (exc_info.value.http_status, exc_info.value.code) == (403, ErrCode.ORDER_NOT_YOURS)

    # ③ 发单人确认 → COMPLETED
    result = service.confirm_receipt(order_id=order_id, user_id=publisher, now=NOW)
    assert result.to_status == Status.COMPLETED.value
    row = _order_row(db, order_id)
    assert row["status"] == Status.COMPLETED.value
    assert row["completed_at"] == NOW
    assert _events(db, order_id)[-1] == "CONFIRM"


# ===========================================================================
# 5. 大厅与详情（读侧）
# ===========================================================================
def test_hall_only_pending_and_never_leaks_pick_code(db: Session, redis_env: Any) -> None:
    """大厅只返回 `PENDING` 且未过期的单，且**响应里没有取件码**（连密文都没有）。

    "查出来再过滤"是不安全的：一个 `logger.info(row)` 就泄露了。
    所以 `_HALL_SQL` 的列清单里根本不含 `pick_code_enc`。
    """
    from app.core.security import encrypt_pick_code

    publisher = _mk_user(db.connection(), "发单人")
    runner = _mk_user(db.connection(), "接单人")
    from_poi = _mk_poi(db.connection(), "驿站", "STATION")
    to_poi = _mk_poi(db.connection(), "宿舍", "DORM")
    repo = SqlOrderRepo(db)

    pending_id = repo.create_order(
        _draft(publisher, from_poi, to_poi, order_type="EXPRESS",
               pick_code_enc=encrypt_pick_code("A-1-101")),
        now=NOW,
    )
    expired_id = repo.create_order(
        _draft(publisher, from_poi, to_poi, deadline=NOW - dt.timedelta(minutes=1)), now=NOW
    )
    taken_id, _p, taken_runner = _new_order(db)
    OrderService(db).grab_order(order_id=taken_id, runner_id=taken_runner, now=NOW)

    items = OrderService(db).hall(user_id=runner, now=NOW)
    ids = {item["id"] for item in items}

    assert pending_id in ids
    assert expired_id not in ids, "已过期的单不该出现在大厅（点了必然 42201）"
    assert taken_id not in ids, "已被接走的单不该出现在大厅"
    assert len(items) <= HALL_LIMIT

    text = repr(items)
    assert "pick_code" not in text, "大厅响应绝不能出现取件码字段"
    assert items[0]["from_poi_name"] and items[0]["to_poi_name"], "列表必须带 POI 名（避免前端 N+1）"
    assert "ACCEPT" in items[0]["available_actions"]


def test_detail_pick_code_visibility(db: Session, redis_env: Any) -> None:
    """取件码只对当事人明文：发单人、已抢单的接单人；陌生人只见掩码。"""
    from app.core.security import encrypt_pick_code

    publisher = _mk_user(db.connection(), "发单人")
    runner = _mk_user(db.connection(), "接单人")
    stranger = _mk_user(db.connection(), "路人")
    from_poi = _mk_poi(db.connection(), "驿站", "STATION")
    to_poi = _mk_poi(db.connection(), "宿舍", "DORM")
    order_id = SqlOrderRepo(db).create_order(
        _draft(publisher, from_poi, to_poi, order_type="EXPRESS",
               pick_code_enc=encrypt_pick_code("B-9-2077")),
        now=NOW,
    )
    service = OrderService(db)

    # ① 抢单前：接单人还不是当事人 —— 看不到明文（否则"抢单"这道门槛形同虚设）
    before = service.detail(order_id=order_id, user_id=runner, now=NOW)
    assert before["pick_code"] is None
    assert before["pick_code_masked"] is True

    # ② 发单人永远可见
    as_publisher = service.detail(order_id=order_id, user_id=publisher, now=NOW)
    assert as_publisher["pick_code"] == "B-9-2077"

    # ③ 抢单后接单人可见
    service.grab_order(order_id=order_id, runner_id=runner, now=NOW)
    after = service.detail(order_id=order_id, user_id=runner, now=NOW)
    assert after["pick_code"] == "B-9-2077"
    assert after["pick_code_masked"] is False

    # ④ 陌生人永远不可见
    as_stranger = service.detail(order_id=order_id, user_id=stranger, now=NOW)
    assert as_stranger["pick_code"] is None
    assert as_stranger["pick_code_masked"] is True


def test_detail_available_actions_by_status_and_role(db: Session, redis_env: Any) -> None:
    """`available_actions[]` 随「状态 × 角色」变化（FR-CENTER-05）。

    **每个 `detail()` 都必须传 `now=NOW`**：详情是守卫层精度，不传就用进程真实时钟，
    而本用例的订单 deadline 是 `NOW + 1h`（2025-06-01）—— 拿 2026 年的"现在"去判，
    订单当然已过期，`ACCEPT` 会被正确地隐藏，用例就会平白无故地红。
    这也正说明守卫层在干活（读的是注入时刻，不是写死的默认值）。
    """
    order_id, publisher, runner = _new_order(db)
    service = OrderService(db)

    pending_runner = service.detail(order_id=order_id, user_id=runner, now=NOW)
    assert pending_runner["available_actions"] == ["ACCEPT"], "PENDING 时接单人能接单"

    pending_publisher = service.detail(order_id=order_id, user_id=publisher, now=NOW)
    assert pending_publisher["available_actions"] == ["CANCEL"], "PENDING 时发单人能取消"

    service.grab_order(order_id=order_id, runner_id=runner, now=NOW)

    in_progress_runner = service.detail(order_id=order_id, user_id=runner, now=NOW)
    assert "PICKED_UP" in in_progress_runner["available_actions"]
    assert "CONFIRM" not in in_progress_runner["available_actions"], (
        "进行中的单不该出现「确认收货」按钮"
    )


def test_detail_missing_order_is_404(db: Session, redis_env: Any) -> None:
    """详情：不存在的订单 → `404/40401`。"""
    with pytest.raises(ApiError) as exc_info:
        OrderService(db).detail(order_id=987_654_322, user_id=1)
    assert exc_info.value.code == ErrCode.ORDER_NOT_FOUND


# ===========================================================================
# 6. 全链路（一个用例走完，验证各段拼在一起仍然成立）
# ===========================================================================
def test_full_lifecycle_end_to_end(db: Session, redis_env: Any) -> None:
    """注册→发单→抢单→取件→配送→送达→确认，状态与事件流逐条对齐。

    单看每个用例都过、拼起来却过不了的情况真实存在（例如 `runner_id` 没写，
    导致 T5/T7 的「接单人本人」守卫全拒 —— 每一段单独测都发现不了）。
    """
    from app.core.security import encrypt_pick_code

    publisher = _mk_user(db.connection(), "发单人")
    runner = _mk_user(db.connection(), "接单人")
    from_poi = _mk_poi(db.connection(), "驿站", "STATION")
    to_poi = _mk_poi(db.connection(), "梅苑1号楼", "DORM")
    repo = SqlOrderRepo(db)
    service = OrderService(db)

    order_id = repo.create_order(
        _draft(publisher, from_poi, to_poi, order_type="EXPRESS",
               pick_code_enc=encrypt_pick_code("8-2-5031")),
        now=NOW,
    )
    assert repo.load_ctx(order_id).status == Status.PENDING.value

    service.grab_order(order_id=order_id, runner_id=runner, now=NOW)
    for action in (Action.PICKED_UP, Action.ON_THE_WAY, Action.DELIVER):
        service.advance_progress(order_id=order_id, user_id=runner, action=action, now=NOW)
    service.confirm_receipt(order_id=order_id, user_id=publisher, now=NOW)

    ctx = repo.load_ctx(order_id)
    assert ctx.status == Status.COMPLETED.value
    assert ctx.runner_id == runner
    assert ctx.accepted_at == NOW
    assert ctx.delivered_at == NOW
    assert ctx.completed_at == NOW
    assert _events(db, order_id) == [
        "CREATE",
        "ACCEPT",
        "PICKED_UP",
        "ON_THE_WAY",
        "DELIVER",
        "CONFIRM",
    ]

    # 投影对账：事件流重放出来的状态必须等于聚合行上的状态（5.4）
    from app.modules.order.state_machine import project_status

    rows = db.execute(
        sa.text(
            "SELECT action, to_status FROM order_progress WHERE order_id = :o ORDER BY seq"
        ),
        {"o": order_id},
    ).all()
    assert project_status([(str(a), str(s)) for a, s in rows]) == ctx.status


# ===========================================================================
# 7. 详情页的 `available_actions[]` 必须是**守卫层**精度
# ===========================================================================
# 这三条是给录屏准备的可视证据：按钮集合必须与"点了会不会成功"完全一致。
# 只看角色层的旧实现会让"已取件"之后再点一次"已取件"→ 42201，画面很难看。
def test_detail_hides_already_done_nodes(db: Session, redis_env: Any) -> None:
    """判据 ①：接单人已 `PICKED_UP` → 详情里**不再**给 `PICKED_UP` / `DELIVER`。

    `DELIVER` 跳级必 `42201`（`_node_in_order` 守卫），`PICKED_UP` 重复点是幂等但
    无意义 —— 两个都不该出现在"当前可做的动作"里。
    """
    order_id, _publisher, runner = _new_order(db)
    service = OrderService(db)
    service.grab_order(order_id=order_id, runner_id=runner, now=NOW)
    service.advance_progress(order_id=order_id, user_id=runner, action=Action.PICKED_UP, now=NOW)

    actions = service.detail(order_id=order_id, user_id=runner, now=NOW)["available_actions"]

    assert "ON_THE_WAY" in actions, f"取件之后唯一下一步是配送中，实得 {actions}"
    assert "PICKED_UP" not in actions, f"已取件不该再出现「已取件」按钮，实得 {actions}"
    assert "DELIVER" not in actions, f"跳级送达必须被隐藏（点了必 42201），实得 {actions}"


def test_detail_t17_available_before_runner_progress(db: Session, redis_env: Any) -> None:
    """判据 ②：发单人 + 接单人**无任何进度** + 静默 ≥45min → `CANCEL_LOST`（T17）可走。

    注意 45 分钟这个前提：T17 的语义是"接单人失联"，刚抢单 1 分钟就允许无责取消
    会把它变成"发单人随时反悔"的漏洞。所以判据是**静默 46 分钟**而非"没进度"。
    """
    order_id, publisher, runner = _new_order(db)
    service = OrderService(db)
    service.grab_order(order_id=order_id, runner_id=runner, now=NOW)

    later = NOW + dt.timedelta(minutes=46)
    actions = service.detail(order_id=order_id, user_id=publisher, now=later)["available_actions"]

    assert "CANCEL_LOST" in actions, f"静默 46 分钟且无进度，T17 必须可走，实得 {actions}"
    assert "DISPUTE_OPENED" not in actions, f"无进度时应走无责取消而不是投诉，实得 {actions}"


def test_detail_t16_replaces_t17_after_runner_progress(db: Session, redis_env: Any) -> None:
    """判据 ③：接单人报过进度 → `CANCEL_LOST` 消失、`DISPUTE_OPENED`（T16）出现。

    这是 T16/T17 的双出口在界面上的样子：**同一个按钮位置，从"无责取消"换成
    "发起投诉"**。两个守卫读同一个 `probe_progress()` 且互为反条件，
    所以任何时刻必有一个返回 False —— 界面上永远只出现一个，不可能同时出现。
    """
    order_id, publisher, runner = _new_order(db)
    service = OrderService(db)
    service.grab_order(order_id=order_id, runner_id=runner, now=NOW)
    service.advance_progress(order_id=order_id, user_id=runner, action=Action.PICKED_UP, now=NOW)

    later = NOW + dt.timedelta(minutes=46)
    actions = service.detail(order_id=order_id, user_id=publisher, now=later)["available_actions"]

    assert "DISPUTE_OPENED" in actions, f"有进度 + 静默 46min，T16 必须可走，实得 {actions}"
    assert "CANCEL_LOST" not in actions, f"接了单还去取件了，不能再无责取消，实得 {actions}"
    assert not {"CANCEL_LOST", "DISPUTE_OPENED"} <= set(actions), "T16/T17 互斥，不能同时出现"


# ===========================================================================
# 8. 我的订单（`/orders/mine`）：与大厅同构 + `my_role`
# ===========================================================================
def test_mine_returns_both_roles_with_my_role_field(db: Session, redis_env: Any) -> None:
    """我发的和我接的都要出现在 `/mine`，且 `my_role` 正确。

    前端 `HallPage` 的两个 Tab 共用同一个解析器（`{items,total}`）与同一张卡片，
    `my_role` 是它区分「发单人视角 / 接单人视角」的**权威值**（优先于按
    `publisher_id` 猜）。所以这个字段错了，页面不会报错 —— 只会把「确认收货」
    按钮画到接单人那一侧，点下去 403。
    """
    publisher = _mk_user(db.connection(), "我的订单发单人")
    runner = _mk_user(db.connection(), "我的订单接单人")
    from_poi = _mk_poi(db.connection(), "驿站", "STATION")
    to_poi = _mk_poi(db.connection(), "宿舍", "DORM")
    repo = SqlOrderRepo(db)
    service = OrderService(db)

    published_id = repo.create_order(_draft(publisher, from_poi, to_poi), now=NOW)
    taken_id = repo.create_order(_draft(publisher, from_poi, to_poi), now=NOW)
    service.grab_order(order_id=taken_id, runner_id=runner, now=NOW)

    mine_as_publisher = {item["id"]: item for item in service.mine(user_id=publisher)}
    mine_as_runner = {item["id"]: item for item in service.mine(user_id=runner)}

    assert published_id in mine_as_publisher
    assert mine_as_publisher[published_id]["my_role"] == "PUBLISHER"
    assert mine_as_runner[taken_id]["my_role"] == "RUNNER"
    # 同一单在两个人的列表里各出现一次、`my_role` 不同 —— 这正是 `my_role` 存在的理由：
    # 光看订单行分不清"我是发单人还是接单人"，而两种身份能做的动作完全不同。
    assert mine_as_publisher[taken_id]["my_role"] == "PUBLISHER", "这单是我发的"
    assert published_id not in mine_as_runner, "没人接的单不该出现在接单人的列表里"

    # 与大厅同构：同一批字段都在（前端同一个解析器）
    item = mine_as_publisher[published_id]
    for key in ("id", "order_no", "status", "type", "reward", "deadline",
                "from_poi_name", "to_poi_name", "created_at", "available_actions", "my_role"):
        assert key in item, f"/mine 缺少字段 {key}（与 /hall 同构的契约被破坏）"
    assert "pick_code" not in repr(item), "列表永不含取件码"
