"""通用内核单测（T-04，验收项 D20）。

覆盖：`ApiError` 结构与 HTTP 状态、统一响应包装、游标编解码往返与非法输入、
`Clock` 注入、`trace_id` 透传与响应头、日志脱敏。

全部零外部依赖（不连库/Redis），`TestClient` 只验证中间件与异常处理器，
不会触发任何数据库访问（`/healthz` 会 ping，故不在此文件里测它的组件状态）。
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from app.core.clock import CAMPUS_TZ_NAME, UTC, Clock
from app.core.errors import (
    CODE_TO_HTTP_STATUS,
    ApiError,
    ErrCode,
    dependency_down,
    http_status_for,
    illegal_transition,
    not_yours,
)
from app.core.logging import (
    TRACE_ID_HEADER,
    get_trace_id,
    new_trace_id,
    reset_trace_id,
    scrub,
    set_trace_id,
)
from app.core.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Cursor,
    build_page,
    decode_cursor,
    encode_cursor,
    normalize_limit,
)
from app.core.response import Page, envelope, fail, ok
from app.main import create_app
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# ApiError
# ---------------------------------------------------------------------------
def test_api_error_structure() -> None:
    err = ApiError(409, ErrCode.ORDER_ALREADY_TAKEN, "手慢了，订单已被接走")
    assert err.http_status == 409
    assert err.code == 40901
    assert err.message == "手慢了，订单已被接走"
    assert err.data is None
    assert err.to_dict() == {
        "code": 40901,
        "data": None,
        "message": "手慢了，订单已被接走",
    }
    # 是 Exception，`raise` / `pytest.raises` 都能用
    assert isinstance(err, Exception)
    assert "40901" in repr(err)


def test_api_error_with_data() -> None:
    err = ApiError(422, ErrCode.ILLEGAL_TRANSITION, "非法迁移", data={"allowed": ["ACCEPT"]})
    assert err.data == {"allowed": ["ACCEPT"]}


@pytest.mark.parametrize(
    "code,expected_status",
    [
        (ErrCode.MISSING_IDEMPOTENCY_KEY, 400),
        (ErrCode.NOT_VERIFIED, 403),
        (ErrCode.ORDER_NOT_YOURS, 403),
        (ErrCode.ORDER_ALREADY_TAKEN, 409),
        (ErrCode.INFLIGHT_LIMIT_REACHED, 409),
        (ErrCode.ORDER_EXPIRED, 410),
        (ErrCode.ILLEGAL_TRANSITION, 422),
        (ErrCode.URGE_LIMIT_REACHED, 429),
        (ErrCode.DEPENDENCY_UNAVAILABLE, 503),
    ],
)
def test_code_to_http_status_segments(code: int, expected_status: int) -> None:
    """K-02 分段规则：每个业务码都能反查到正确的 HTTP 状态。"""
    assert http_status_for(code) == expected_status
    assert CODE_TO_HTTP_STATUS[code] == expected_status


def test_unknown_code_defaults_to_400_not_500() -> None:
    """未知码不能默认 500 —— 那会把用户错误报成系统故障，误导告警。"""
    assert http_status_for(999999) == 400


def test_error_code_values_are_stable() -> None:
    """错误码是前后端契约，改动即破坏兼容 —— 这里锁死任务书要求的 9 个码值。"""
    assert ErrCode.ORDER_ALREADY_TAKEN == 40901
    assert ErrCode.INFLIGHT_LIMIT_REACHED == 40902
    assert ErrCode.ORDER_EXPIRED == 41001
    assert ErrCode.NOT_VERIFIED == 40301
    assert ErrCode.ORDER_NOT_YOURS == 40310
    assert ErrCode.ILLEGAL_TRANSITION == 42201
    assert ErrCode.PENDING_LIMIT_REACHED == 42902
    assert ErrCode.URGE_LIMIT_REACHED == 42903
    assert ErrCode.DEPENDENCY_UNAVAILABLE == 50300
    # 文档 6.3 表格里的抢单错误码
    assert ErrCode.ORDER_STATUS_NOT_GRABBABLE == 40903
    assert ErrCode.IDEMPOTENCY_KEY_CONFLICT == 40904
    assert ErrCode.STATUS_CHANGED_CONCURRENTLY == 40905


def test_error_helpers() -> None:
    assert illegal_transition().code == 42201
    assert not_yours().http_status == 403
    assert dependency_down("数据库").http_status == 503


# ---------------------------------------------------------------------------
# 统一响应
# ---------------------------------------------------------------------------
def test_ok_envelope_has_four_keys() -> None:
    body = ok({"id": 1})
    assert set(body) == {"code", "data", "message", "trace_id"}
    assert body["code"] == 0
    assert body["data"] == {"id": 1}
    assert body["message"] == "ok"


def test_fail_envelope() -> None:
    body = fail(40901, "手慢了")
    assert body["code"] == 40901
    assert body["data"] is None
    assert body["message"] == "手慢了"


def test_envelope_serializes_decimal_as_string() -> None:
    """金额必须走字符串：JS 的 Number 会把 12.30 变成 12.300000000000001。"""
    body = ok({"reward": decimal.Decimal("12.30")})
    assert body["data"]["reward"] == "12.30"


def test_envelope_serializes_datetime_with_z() -> None:
    dt = datetime.datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    body = ok({"t": dt})
    assert body["data"]["t"] == "2025-06-01T12:00:00Z"

    naive = datetime.datetime(2025, 6, 1, 12, 0)
    assert ok({"t": naive})["data"]["t"] == "2025-06-01T12:00:00Z"


def test_envelope_nested_structure() -> None:
    body = ok([{"a": decimal.Decimal("2.50"), "b": [decimal.Decimal("1.00")]}])
    assert body["data"] == [{"a": "2.50", "b": ["1.00"]}]


def test_envelope_explicit_trace_id() -> None:
    body = envelope(0, None, "ok", trace_id="abc123")
    assert body["trace_id"] == "abc123"


def test_page_payload() -> None:
    page = Page(items=[1, 2], next_cursor="c1", has_more=True)
    body = page.to_response()
    assert body["code"] == 0
    assert body["data"] == {"items": [1, 2], "next_cursor": "c1", "has_more": True}


# ---------------------------------------------------------------------------
# 游标分页
# ---------------------------------------------------------------------------
def test_cursor_roundtrip() -> None:
    ts = datetime.datetime(2025, 6, 1, 12, 34, 56, 789012, tzinfo=UTC)
    token = encode_cursor(ts, 4242)
    assert isinstance(token, str) and token
    decoded = decode_cursor(token)
    assert decoded is not None
    assert decoded.id == 4242
    assert decoded.created_at == ts
    assert decoded.as_tuple() == (ts, 4242)


def test_cursor_roundtrip_naive_input_is_treated_as_utc() -> None:
    naive = datetime.datetime(2025, 6, 1, 12, 0)
    decoded = decode_cursor(encode_cursor(naive, 7))
    assert decoded is not None
    assert decoded.created_at == naive.replace(tzinfo=UTC)
    assert decoded.created_at.tzinfo is not None


def test_cursor_roundtrip_local_timezone() -> None:
    """非 UTC 的 aware 时间会先归一化到 UTC，再编码。"""
    tz8 = datetime.timezone(datetime.timedelta(hours=8))
    local = datetime.datetime(2025, 6, 1, 20, 0, tzinfo=tz8)
    decoded = decode_cursor(encode_cursor(local, 1))
    assert decoded is not None
    assert decoded.created_at == datetime.datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "not-base64!!!",
        "YWJj",                      # "abc"，无分隔符
        "fHNvbWU=",                  # "|some"，时间戳为空
        "MjAyNS0wNi0wMVQxMjowMDowMCswMDowMHxhYmM=",  # id 不是数字
        "Zm9vLWJhcnwxMjM=",          # 时间戳不是 ISO
        "8J+Zgg==",                  # emoji，解出来不是 ASCII
        "x" * 5000,                  # 超长垃圾
    ],
)
def test_decode_cursor_invalid_returns_none(bad: str | None) -> None:
    """非法游标**返回 None 而不是抛异常**：用户把 URL 改坏了应退化成"从第一页开始"。"""
    assert decode_cursor(bad) is None


def test_decode_cursor_negative_id() -> None:
    assert decode_cursor(encode_cursor(datetime.datetime(2025, 6, 1, tzinfo=UTC), -1)) is None


def test_decode_cursor_missing_padding_is_tolerated() -> None:
    """前端/网关截断 base64 的 '=' 是常见现象，必须能解。"""
    ts = datetime.datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    token = encode_cursor(ts, 9)
    assert decode_cursor(token.rstrip("=")) is not None


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, DEFAULT_LIMIT),
        (0, DEFAULT_LIMIT),
        (-5, DEFAULT_LIMIT),
        (1, 1),
        (20, 20),
        (50, 50),
        (999, MAX_LIMIT),
        ("30", 30),
        ("abc", DEFAULT_LIMIT),
    ],
)
def test_normalize_limit(value: object, expected: int) -> None:
    assert normalize_limit(value) == expected  # type: ignore[arg-type]


class _Row:
    __slots__ = ("created_at", "id")

    def __init__(self, created_at: datetime.datetime, id_: int) -> None:
        self.created_at = created_at
        self.id = id_


def _cursor_of(row: object) -> tuple[datetime.datetime, int]:
    assert isinstance(row, _Row)
    return row.created_at, row.id


def test_build_page_has_more() -> None:
    """多取一条判 has_more：不需要 count(*)。"""
    rows = [_Row(datetime.datetime(2025, 6, 1, 12, i, tzinfo=UTC), 100 - i) for i in range(3)]
    items, next_cursor, has_more = build_page(rows, limit=2, cursor_of=_cursor_of)
    assert len(items) == 2
    assert has_more is True
    assert next_cursor is not None
    decoded = decode_cursor(next_cursor)
    assert decoded is not None and decoded.id == 99


def test_build_page_last_page() -> None:
    rows = [_Row(datetime.datetime(2025, 6, 1, 12, i, tzinfo=UTC), 100 - i) for i in range(2)]
    items, next_cursor, has_more = build_page(rows, limit=2, cursor_of=_cursor_of)
    assert len(items) == 2
    assert has_more is False
    assert next_cursor is None


def test_build_page_empty() -> None:
    items, next_cursor, has_more = build_page([], limit=20, cursor_of=_cursor_of)
    assert (items, next_cursor, has_more) == ([], None, False)


def test_cursor_is_frozen() -> None:
    cursor = Cursor(created_at=datetime.datetime(2025, 6, 1, tzinfo=UTC), id=1)
    with pytest.raises((AttributeError, TypeError)):
        cursor.id = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------
def test_clock_real_is_utc_aware() -> None:
    now = Clock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == datetime.timedelta(0)


def test_clock_fixed_injection() -> None:
    """固定时钟让「45 分钟边界」这类用例不需要 sleep。"""
    fixed = datetime.datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    clock = Clock(fixed=fixed)
    assert clock.now() == fixed
    assert clock.is_fixed is True
    clock.advance(datetime.timedelta(minutes=45))
    assert clock.now() == fixed + datetime.timedelta(minutes=45)
    clock.reset()
    assert clock.is_fixed is False
    assert clock.now() != fixed


def test_clock_naive_fixed_is_coerced_to_utc() -> None:
    clock = Clock(fixed=datetime.datetime(2025, 6, 1, 12, 0))
    assert clock.now().tzinfo is UTC


def test_clock_to_local() -> None:
    """UTC 04:00 → 校园时区 12:00（东八区）。"""
    clock = Clock(fixed=datetime.datetime(2025, 6, 1, 4, 0, tzinfo=UTC))
    local = clock.to_local(clock.now())
    assert (local.hour, local.minute) == (12, 0)
    assert local.utcoffset() == datetime.timedelta(hours=8)
    assert clock.today_local() == datetime.date(2025, 6, 1)


def test_clock_to_local_crosses_date() -> None:
    """UTC 2025-06-01 20:00 → 校园 2025-06-02 04:00（跨日，发单窗要按当地时间判）。"""
    clock = Clock(fixed=datetime.datetime(2025, 6, 1, 20, 0, tzinfo=UTC))
    local = clock.to_local(clock.now())
    assert local.date() == datetime.date(2025, 6, 2)
    assert local.hour == 4


def test_campus_tz_name() -> None:
    assert CAMPUS_TZ_NAME == "Asia/Shanghai"


# ---------------------------------------------------------------------------
# trace_id 与脱敏
# ---------------------------------------------------------------------------
def test_trace_id_contextvar() -> None:
    assert get_trace_id() == ""
    token = set_trace_id("abc123")
    try:
        assert get_trace_id() == "abc123"
    finally:
        reset_trace_id(token)
    assert get_trace_id() == ""


def test_new_trace_id_is_unique_and_hex() -> None:
    a, b = new_trace_id(), new_trace_id()
    assert a != b
    assert len(a) == 32
    int(a, 16)  # 不抛异常即为合法 hex


def test_scrub_masks_sensitive_fields() -> None:
    """K-07：取件码、手机号、学号属于「禁止出现」级别。"""
    cleaned = scrub(
        {
            "order_no": "CR250601123456",
            "pick_code": "8-2-1234",
            "pick_code_enc": b"\x00",
            "phone": "13800008888",
            "phone_mask": "138****8888",
            "student_no": "20210001",
            "nested": {"pick_code": "secret", "note": "ok"},
        }
    )
    assert cleaned["order_no"] == "CR250601123456"
    assert cleaned["pick_code"] == "***"
    assert cleaned["pick_code_enc"] == "***"
    assert cleaned["phone"] == "***"
    assert cleaned["student_no"] == "***"
    # 打码后的字段本身是可以出现的
    assert cleaned["phone_mask"] == "138****8888"
    assert cleaned["nested"] == {"pick_code": "***", "note": "ok"}


def test_scrub_none() -> None:
    assert scrub(None) == {}


# ---------------------------------------------------------------------------
# 应用接线：中间件、异常处理器、响应头
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client() -> TestClient:
    """不进入 lifespan（不触发 engine.dispose 之外的任何副作用）。

    `/healthz` 会 ping 依赖，本文件不调用它 —— 组件状态属于集成测试。
    """
    return TestClient(create_app(), raise_server_exceptions=False)


def test_trace_id_generated_and_echoed_in_header(client: TestClient) -> None:
    response = client.get("/__trace_probe")
    assert response.status_code == 200
    trace_id = response.headers.get(TRACE_ID_HEADER)
    assert trace_id  # 缺失则新建
    assert response.json()["data"]["trace_id"] == trace_id
    assert response.json()["trace_id"] == trace_id


def test_trace_id_passthrough(client: TestClient) -> None:
    """外部传入的 trace_id 必须透传（前端/网关可串联）。"""
    given = "abcdef0123456789"
    response = client.get("/__trace_probe", headers={TRACE_ID_HEADER: given})
    assert response.headers[TRACE_ID_HEADER] == given
    assert response.json()["data"]["trace_id"] == given


def test_invalid_trace_id_is_replaced(client: TestClient) -> None:
    """非法 trace_id（过短/含非法字符）不采信，避免日志注入。"""
    response = client.get("/__trace_probe", headers={TRACE_ID_HEADER: "bad!!"})
    returned = response.headers[TRACE_ID_HEADER]
    assert returned != "bad!!"
    assert len(returned) == 32


def test_api_error_handler_returns_unified_body() -> None:
    """`ApiError(409, 40901)` 必须输出统一结构 + 正确 HTTP 状态 + trace_id。"""
    app = create_app()

    @app.get("/__boom_business", include_in_schema=False)
    def _boom() -> None:
        raise ApiError(409, ErrCode.ORDER_ALREADY_TAKEN, "手慢了，订单已被接走")

    with TestClient(app) as client:
        response = client.get("/__boom_business", headers={TRACE_ID_HEADER: "trace-0001"})
    assert response.status_code == 409
    body = response.json()
    assert set(body) == {"code", "data", "message", "trace_id"}
    assert body["code"] == 40901
    assert body["message"] == "手慢了，订单已被接走"
    assert body["trace_id"] == "trace-0001"
    assert response.headers[TRACE_ID_HEADER] == "trace-0001"


def test_unhandled_exception_hides_stacktrace() -> None:
    """兜底 500：**只回 trace_id，不回堆栈/内部信息**。"""
    app = create_app()

    @app.get("/__boom_unhandled", include_in_schema=False)
    def _boom() -> None:
        raise RuntimeError("connection to db at 10.0.0.5 refused")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/__boom_unhandled", headers={TRACE_ID_HEADER: "trace-5000"})
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == ErrCode.INTERNAL_ERROR
    assert body["trace_id"] == "trace-5000"
    # 不泄露：内部信息一个字都不能出现
    assert "10.0.0.5" not in body["message"]
    assert "RuntimeError" not in body["message"]
    assert "Traceback" not in str(body)


def test_404_still_has_trace_header(client: TestClient) -> None:
    """路由不匹配（框架自己产生的响应）也必须带 X-Trace-Id。"""
    response = client.get("/__no_such_route")
    assert response.status_code == 404
    assert response.headers.get(TRACE_ID_HEADER)


def test_cors_exposes_trace_header(client: TestClient) -> None:
    """CORS 必须 expose X-Trace-Id，否则前端 JS 读不到它。"""
    response = client.get(
        "/__trace_probe",
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.headers.get("access-control-expose-headers", "").lower().find(
        "x-trace-id"
    ) >= 0
