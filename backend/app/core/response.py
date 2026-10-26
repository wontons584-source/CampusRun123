"""统一响应包装（T-04）。

所有 HTTP 响应（**包括错误**）都是 `{code, data, message, trace_id}`：

- `code=0` 成功；非 0 见 `app.core.errors.ErrCode`；
- `data` 成功时是业务数据，失败时可为 None 或结构化补充信息；
- `message` 给人看，可以改；`code` 给机器用，是契约；
- `trace_id` 用于用户报障时定位 —— 所以**错误响应也必须带**，
  否则用户说「报错了」时手上没有任何线索。

为什么不用 FastAPI 默认的 `HTTPException` 结构：
它返回 `{"detail": "..."}`，前端要为「业务错误」与「框架错误」写两套解析，
而且无法携带业务码。统一包装后前端只有一个错误分支。
"""

from __future__ import annotations

import datetime
import decimal
from typing import Any, Generic, TypeVar

from app.core.logging import get_trace_id

T = TypeVar("T")

# 响应体的固定键（前端与 api-client 生成都依赖这个顺序/键名，别改名）
CODE: str = "code"
DATA: str = "data"
MESSAGE: str = "message"
TRACE_ID: str = "trace_id"


def _jsonable(value: Any) -> Any:
    """把常见非 JSON 原生类型转成可序列化形式。

    `Decimal` → `str`：金额用字符串传，避免 JS `Number` 的浮点精度问题
      （12.30 变成 12.300000000000001 这种）。
    `datetime` → ISO 8601 带 `Z`（K-04：接口一律 ISO 8601 带 Z）。
    """
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            # 不该发生（DB 全是 timestamptz），但兜底不抛错
            return value.replace(tzinfo=datetime.UTC).isoformat().replace("+00:00", "Z")
        return value.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def envelope(
    code: int,
    data: Any = None,
    message: str = "ok",
    *,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """底层包装：其他函数都走这里，保证键名与序列化规则只有一处。"""
    return {
        CODE: code,
        DATA: _jsonable(data),
        MESSAGE: message,
        TRACE_ID: trace_id if trace_id is not None else get_trace_id(),
    }


def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    """成功响应。`code` 恒为 0。"""
    return envelope(0, data, message)


def fail(code: int, message: str, *, data: Any = None, trace_id: str | None = None) -> dict[str, Any]:
    """失败响应。`code` 必须是 `ErrCode` 里的常量，禁止手写魔法数字。"""
    return envelope(code, data, message, trace_id=trace_id)


class Page(Generic[T]):
    """游标分页响应体（K-05）：`{items, next_cursor, has_more}`。

    **禁止** `page/page_size`：深 OFFSET 在 10 万行时会全表扫，
    而游标分页的代价与页深无关。
    """

    __slots__ = ("items", "next_cursor", "has_more")

    def __init__(
        self,
        items: list[T],
        next_cursor: str | None = None,
        has_more: bool = False,
    ) -> None:
        self.items: list[T] = items
        self.next_cursor: str | None = next_cursor
        self.has_more: bool = has_more

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": _jsonable(self.items),
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
        }

    def to_response(self, message: str = "ok") -> dict[str, Any]:
        return ok(self.to_dict(), message)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Page items={len(self.items)} has_more={self.has_more}>"
