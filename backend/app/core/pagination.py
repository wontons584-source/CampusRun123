"""游标分页（T-04, K-05）。

约定：请求 `cursor` + `limit`（默认 20，上限 50），响应 `{items, next_cursor, has_more}`。
**禁止 `page/page_size`**：深 OFFSET 在待接单量暴涨时要全表扫，
而游标分页的代价与页深无关（`WHERE (created_at, id) < ($1, $2)` 直接走索引）。

游标编码：`base64url(f"{created_at.isoformat()}|{id}")`。
为什么带 `id` 而不只用 `created_at`：同一毫秒可能有多条（批量导入/并发发单），
只用时间戳做游标会**丢行或重复行**。`(created_at DESC, id DESC)` 才是全序，
这也是 5.3 里 `orders(publisher_id, created_at DESC, id DESC)` 建联合索引的原因。

安全与容错：
- 游标是**不可信输入**（用户可以随便改）。`decode_cursor` 对任何异常都返回 `None`
  而不是抛 500 —— 用户把 URL 改坏了应该退化成「从第一页开始」，而不是白屏报错。
- 不做签名/加密：游标里只有时间戳和自增 id，已经是接口能看到的公开信息；
  加密反而让分页无法被日志与工单复现。
"""

from __future__ import annotations

import base64
import binascii
import datetime
from collections.abc import Callable
from dataclasses import dataclass

DEFAULT_LIMIT: int = 20
MAX_LIMIT: int = 50

_SEPARATOR: str = "|"


@dataclass(frozen=True, slots=True)
class Cursor:
    """解码后的游标：上一页最后一行的 `(created_at, id)`。"""

    created_at: datetime.datetime
    id: int

    def as_tuple(self) -> tuple[datetime.datetime, int]:
        return (self.created_at, self.id)


def encode_cursor(created_at: datetime.datetime, id_: int) -> str:
    """编码游标。

    naive datetime 视为 UTC（DB 全是 timestamptz，naive 只会来自错误构造）；
    这里不抛错是为了让「一条脏数据」不至于让整个列表接口 500。
    """
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.UTC)
    else:
        created_at = created_at.astimezone(datetime.UTC)
    raw = f"{created_at.isoformat()}{_SEPARATOR}{int(id_)}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str | None) -> Cursor | None:
    """解码游标；**任何非法输入都返回 `None`**（等价「从头开始」），绝不抛异常。

    覆盖的非法情形：空值、非 base64、缺少分隔符、时间戳格式错、id 非数字、
    时间戳被塞了 `; DROP TABLE`、超长串等。
    """
    if not cursor or not isinstance(cursor, str):
        return None
    try:
        # 补齐 base64 padding（前端可能截断 '='，URL 里也常见）
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if _SEPARATOR not in raw:
        return None
    ts_part, _, id_part = raw.rpartition(_SEPARATOR)
    if not ts_part or not id_part:
        return None
    try:
        created_at = datetime.datetime.fromisoformat(ts_part)
    except ValueError:
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=datetime.UTC)
    try:
        cursor_id = int(id_part)
    except (TypeError, ValueError):
        return None
    if cursor_id < 0:
        return None
    return Cursor(created_at=created_at, id=cursor_id)


def normalize_limit(limit: int | None) -> int:
    """限制取值：默认 20、上限 50、下限 1。非法值不抛错，按默认处理。"""
    if limit is None:
        return DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    if value <= 0:
        return DEFAULT_LIMIT
    return min(value, MAX_LIMIT)


def build_page(
    rows: list[object],
    limit: int,
    *,
    cursor_of: Callable[[object], tuple[datetime.datetime, int]],
) -> tuple[list[object], str | None, bool]:
    """把「多取一条」的查询结果切成 `(items, next_cursor, has_more)`。

    标准做法：`LIMIT limit+1`，若真取到 limit+1 条说明还有下一页，
    丢弃第 limit+1 条并用第 limit 条构造游标 —— 这样**不需要 count(*)**
    （count 在 10 万行表上比查询本身还贵）。

    Args:
        rows: 已按 `created_at DESC, id DESC` 排序的原始行（最多 limit+1 条）。
        limit: 页大小（已 normalize）。
        cursor_of: 从一行里取 `(created_at, id)` 的函数。
    """
    has_more = len(rows) > limit
    items = list(rows[:limit])
    if not has_more or not items:
        return items, None, False
    created_at, row_id = cursor_of(items[-1])
    return items, encode_cursor(created_at, row_id), True
