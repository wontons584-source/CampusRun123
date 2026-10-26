"""纠纷聚合根：disputes。

BR-28：一单最多一次纠纷 → `UNIQUE(order_id)`。
M1 的 `status` 只有 `OPEN / RESOLVED`（无举证-申诉中间态，T16 更是免举证直接冻结）。
**M3 前不要往本表塞临时字段**（技术债已记账）：加状态时另开 `dispute_evidences` 表。

`result` 与订单状态机的对应关系（BR-35）：
- `FOR_RUNNER`     → T13（PRE=IN_PROGRESS）或 T14（PRE=DELIVERED/COMPLETED）回滚
- `FOR_PUBLISHER`  → T15 担责 → CANCELLED
- `PARTIAL`        → M1 按 T15 处理（钱不归平台管，C2），M3 再做分档
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

DISPUTE_REASONS: tuple[str, ...] = (
    "NOT_DELIVERED",
    "DAMAGED",
    "MISMATCH",
    "FAKE_DELIVER",
    "BAD_ATTITUDE",
    "RUNNER_LOST",
)

DISPUTE_STATUSES: tuple[str, ...] = ("OPEN", "RESOLVED")

DISPUTE_RESULTS: tuple[str, ...] = ("FOR_PUBLISHER", "FOR_RUNNER", "PARTIAL")


class Dispute(Base, TimestampMixin):
    """纠纷（5.2 disputes，BR-28 一单一次）。"""

    __tablename__ = "disputes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE", name="fk_disputes_order_id_orders"),
        nullable=False,
        comment="BR-28 一单一次（UNIQUE）",
    )
    initiator_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_disputes_initiator_id_users"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="NOT_DELIVERED/DAMAGED/MISMATCH/FAKE_DELIVER/BAD_ATTITUDE/RUNNER_LOST",
    )
    evidence: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment='{images:[...], text:""} ≤5 图 ≤300 字；T16 免举证'
    )
    status: Mapped[str] = mapped_column(
        String(14),
        nullable=False,
        comment="M1: OPEN → RESOLVED；M3 加 EVIDENCE/ARBITRATING/APPEALED",
    )
    result: Mapped[str | None] = mapped_column(
        # 列宽 16（不是 5.2 写的 14）：`FOR_PUBLISHER`(13) 在 varchar(14) 下余量只有 1
        String(16), nullable=True, comment="FOR_PUBLISHER/FOR_RUNNER/PARTIAL"
    )
    result_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    arbitrator_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="裁决管理员 users.id（不加 FK，见 5.2）"
    )
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # 5.3：一单一次纠纷 + 纠纷队列
        Index("uq_disputes_order_id", "order_id", unique=True),
        Index("ix_disputes_status_created_at", "status", "created_at"),
        CheckConstraint(
            "status IN ('OPEN', 'RESOLVED')", name="status_enum"
        ),
        CheckConstraint(
            "reason IN ('NOT_DELIVERED', 'DAMAGED', 'MISMATCH', 'FAKE_DELIVER', "
            "'BAD_ATTITUDE', 'RUNNER_LOST')",
            name="reason_enum",
        ),
        CheckConstraint(
            "result IS NULL OR result IN ('FOR_PUBLISHER', 'FOR_RUNNER', 'PARTIAL')",
            name="result_enum",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Dispute order_id={self.order_id} status={self.status} result={self.result}>"
