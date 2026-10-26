"""测试夹具（T-04 / T-12）。

两条硬要求：
1. **不连库、不连 Redis**（C1/C2）。所有测试用内存替身（`MemoryOrderRepo`）。
2. `sys.path` 里必须能找到 `app` 包 —— 本文件在 `backend/tests/` 下，
   父目录是 `backend/`，显式插入而不是依赖 `pytest.ini` 的 `pythonpath`
   （后者在旧版 pytest 上不存在）。
"""

from __future__ import annotations

import datetime
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.clock import UTC, Clock  # noqa: E402
from app.modules.order.state_machine import (  # noqa: E402
    ProgressEvent,
    Thresholds,
    TransitionPayload,
)

# 固定基准时刻：2025-06-01 12:00 UTC = 20:00 Asia/Shanghai（在 BR-10 发单窗内）
BASE_NOW: datetime.datetime = datetime.datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


class MemoryOrderRepo:
    """`OrderRepo` 协议的内存实现（单测替身，不参与生产代码）。

    刻意模拟真实 repo 的两个语义：
    - `append_progress` 分配单调 `seq`（DB 侧是 `MAX(seq)+1`，真实实现由 repo 保证）；
    - `cas_update_status` 只有 `当前状态 == expected_status` 才生效，返回 0/1 行。
    """

    def __init__(self) -> None:
        self.events: list[tuple[ProgressEvent, datetime.datetime]] = []
        self.rows: dict[int, dict[str, Any]] = {}
        self._seq: int = 0
        self.fail_next_cas: bool = False

    # ---- OrderRepo 协议 ----
    def append_progress(self, event: ProgressEvent, *, created_at: datetime.datetime) -> int:
        self._seq += 1
        self.events.append((event, created_at))
        return self._seq

    def cas_update_status(
        self,
        *,
        order_id: int,
        expected_status: str,
        to_status: str,
        patch: Mapping[str, Any],
    ) -> int:
        row = self.rows.setdefault(order_id, {"status": expected_status})
        if self.fail_next_cas:
            self.fail_next_cas = False
            return 0
        if row["status"] != expected_status:
            return 0
        row["status"] = to_status
        row.update(patch)
        return 1

    # ---- 断言辅助 ----
    def seed(self, order_id: int, status: str) -> None:
        self.rows[order_id] = {"status": status}

    @property
    def actions(self) -> list[str]:
        return [event.action.value for event, _ in self.events]

    @property
    def status_of(self) -> dict[int, str]:
        return {oid: str(row["status"]) for oid, row in self.rows.items()}


def make_clock(now: datetime.datetime = BASE_NOW) -> Clock:
    """固定时钟：让「45 分钟边界」这类用例不需要 sleep。"""
    return Clock(fixed=now)


__all__ = [
    "BASE_NOW",
    "Clock",
    "MemoryOrderRepo",
    "ProgressEvent",
    "Thresholds",
    "TransitionPayload",
    "make_clock",
]
