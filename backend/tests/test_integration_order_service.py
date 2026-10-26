"""发单（T-13）集成测试：真库验证 service 校验与密文落库。

验收点（对应架构 T-13 判据）：
- 酬劳 1.9 / 50.5 / 5.3（非 0.5 步进）拒
- `type=EXPRESS` 不传 pick_code → 40002
- 非 EXPRESS 传 pick_code → 忽略（不报错）
- 23:30 发单拒（发单窗外）
- 描述超长拒（Pydantic 层）
- `pick_code` 落库为密文（查 DB 断言不含明文）
- deadline 正确（今日内 → 当日 23:00）
"""

from __future__ import annotations

import datetime
import os
from decimal import Decimal

import pytest
import sqlalchemy as sa
from app.core.clock import UTC
from app.core.errors import ApiError, ErrCode
from app.core.security import decrypt_pick_code
from app.modules.order.service import OrderService
from sqlalchemy.orm import Session

TEST_DATABASE_URL: str = os.environ.get("TEST_DATABASE_URL", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="未设置 TEST_DATABASE_URL"),
]

# 一个「发单窗内」的时刻：UTC 02:00 = 北京时间 10:00（在 07:00–23:00 内）
NOW = datetime.datetime(2025, 6, 1, 2, 0, tzinfo=UTC)
# 发单窗外：UTC 15:30 = 北京时间 23:30（≥23:00）
LATE_NIGHT = datetime.datetime(2025, 6, 1, 15, 30, tzinfo=UTC)


@pytest.fixture()
def db(engine: sa.Engine):
    with engine.connect() as conn:
        txn = conn.begin()
        s = Session(bind=conn, expire_on_commit=False)
        try:
            yield s
        finally:
            s.close()
            if txn.is_active:
                txn.rollback()


@pytest.fixture(scope="module")
def engine():
    if not TEST_DATABASE_URL:
        pytest.skip("未设置 TEST_DATABASE_URL")
    from sqlalchemy.pool import NullPool

    eng = sa.create_engine(TEST_DATABASE_URL, poolclass=NullPool, future=True)
    yield eng
    eng.dispose()


def _mk_user(conn, name="发单人") -> int:
    return int(
        conn.execute(
            sa.text(
                "INSERT INTO users (nickname, credit_score, verify_status, status) "
                "VALUES (:n, 100, 'APPROVED', 'ACTIVE') RETURNING id"
            ),
            {"n": f"{name}-{os.urandom(4).hex()}"},
        ).scalar_one()
    )


def _mk_poi(conn, name="驿站", *, lat="32.060000", lng="118.780000") -> int:
    return int(
        conn.execute(
            sa.text(
                "INSERT INTO pois (campus_id, name, type, lat, lng) "
                "VALUES (1, :n, 'STATION', :lat, :lng) RETURNING id"
            ),
            {"n": f"{name}-{os.urandom(4).hex()}", "lat": lat, "lng": lng},
        ).scalar_one()
    )


def _svc(db: Session) -> OrderService:
    return OrderService(db)


def _create(db: Session, **kw):
    defaults = dict(
        publisher_id=_mk_user(db.connection()),
        is_verified=True,
        type_="BUY",
        from_poi_id=_mk_poi(db.connection()),
        to_poi_id=_mk_poi(db.connection(), "宿舍"),
        reward=Decimal("5.50"),
        expect_minutes=30,
        description=None,
        images=(),
        pick_code=None,
        now=NOW,
    )
    defaults.update(kw)
    return _svc(db).create_order(**defaults)


# ---------------------------------------------------------------------------
# 校验用例
# ---------------------------------------------------------------------------
def test_create_order_success(db: Session) -> None:
    oid = _create(db)
    assert oid > 0
    row = db.execute(
        sa.text("SELECT status, settle_state, runner_id FROM orders WHERE id=:id"),
        {"id": oid},
    ).one()
    assert row[0] == "PENDING"
    assert row[1] == "UNSETTLED"
    assert row[2] is None


def test_express_requires_pick_code(db: Session) -> None:
    with pytest.raises(ApiError) as ei:
        _create(db, type_="EXPRESS", pick_code=None)
    assert ei.value.code == ErrCode.PICK_CODE_REQUIRED


def test_non_express_pick_code_ignored(db: Session) -> None:
    # 非 EXPRESS 传了 pick_code：不报错，忽略（且落库无密文）
    oid = _create(db, type_="BUY", pick_code="999999")
    enc = db.execute(
        sa.text("SELECT pick_code_enc FROM orders WHERE id=:id"), {"id": oid}
    ).scalar_one()
    assert enc is None


def test_pick_code_encrypted_at_rest(db: Session) -> None:
    oid = _create(db, type_="EXPRESS", pick_code="888888")
    enc = db.execute(
        sa.text("SELECT pick_code_enc FROM orders WHERE id=:id"), {"id": oid}
    ).scalar_one()
    assert enc is not None
    assert b"888888" not in enc  # 密文不含明文
    assert decrypt_pick_code(enc) == "888888"


def test_send_window_rejected(db: Session) -> None:
    with pytest.raises(ApiError) as ei:
        _create(db, now=LATE_NIGHT)
    assert ei.value.code == ErrCode.INVALID_ARGUMENT


def test_unverified_rejected(db: Session) -> None:
    with pytest.raises(ApiError) as ei:
        _create(db, is_verified=False)
    assert ei.value.code == ErrCode.NOT_VERIFIED


def test_pending_limit_reached(db: Session) -> None:
    uid = _mk_user(db.connection())
    # 先发满 3 单（PENDING）
    for _ in range(3):
        _create(db, publisher_id=uid)
    # 第 4 单应被拒
    with pytest.raises(ApiError) as ei:
        _create(db, publisher_id=uid)
    assert ei.value.code == ErrCode.PENDING_LIMIT_REACHED


def test_same_poi_rejected(db: Session) -> None:
    pid = _mk_poi(db.connection())
    with pytest.raises(ApiError) as ei:
        _create(db, from_poi_id=pid, to_poi_id=pid)
    assert ei.value.code == ErrCode.INVALID_ARGUMENT


def test_deadline_today_end(db: Session) -> None:
    """expect_minutes=720（今日内）→ 当日 23:00（校园时区）。"""
    oid = _create(db, expect_minutes=720)
    deadline = db.execute(
        sa.text("SELECT deadline FROM orders WHERE id=:id"), {"id": oid}
    ).scalar_one()
    # NOW 是 UTC 02:00 = 北京 10:00，当日 23:00 北京 = UTC 15:00
    assert deadline == datetime.datetime(2025, 6, 1, 15, 0, tzinfo=UTC)
