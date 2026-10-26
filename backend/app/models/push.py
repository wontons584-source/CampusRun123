"""推送与通知聚合根：push_logs / notifications。

- `push_logs` 只做审计与调参（FR-PUSH-08）；频控另走 Redis，不进这张表。
  降级也要留痕（`channel=HALL_ONLY`，D17），否则「为什么这个人没收到推送」
  在上线后完全无法复盘。
- `notifications` 是**必达主通道**（FR-PUSH-06）：推送可以丢，站内信不能丢。
  红点 = `read_at IS NULL` 的行数，因此索引必须带上 `read_at`。
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin

# 推送通道（含降级留痕，D17）
PUSH_CHANNELS: tuple[str, ...] = ("INBOX", "PUSH", "HALL_ONLY")

# 站内信类型（FR-PUSH-06 及各模块通知点）
NOTIFICATION_KINDS: tuple[str, ...] = (
    "PUSH_NEW_ORDER",
    "ACCEPTED",
    "PROGRESS",
    "DELIVERED",
    "NEED_CONFIRM",
    "AUTO_CONFIRMED",
    "DISPUTE_OPENED",
    "DISPUTE_RESOLVED",
    "CREDIT",
    "VERIFY_RESULT",
    "BAN",
    "RUNNER_SILENT_WARN",
    "CAN_SELF_CANCEL",
    "CANCEL_REQUEST",
)


class PushLog(Base, CreatedAtMixin):
    """推送轨迹（5.2 push_logs）。

    `round` 是 SQL 函数名但不是保留字，PG 中可直接做列名（必要时由方言加引号）。
    """

    __tablename__ = "push_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE", name="fk_push_logs_order_id_orders"),
        nullable=False,
    )
    runner_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_push_logs_runner_id_users"),
        nullable=False,
    )
    round: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="扩圈轮次 1/2/3（BR-14）"
    )
    channel: Mapped[str] = mapped_column(
        String(12), nullable=False, comment="INBOX/PUSH/HALL_ONLY（降级也留痕，D17）"
    )
    distance_m: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="到召回键距离（米）"
    )
    score: Mapped[Any | None] = mapped_column(
        Numeric(6, 4), nullable=True, comment="打分值，供调参（FR-PUSH-08）"
    )
    is_clicked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="效果回流"
    )
    is_grabbed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="效果回流"
    )

    __table_args__ = (
        # 5.3：频控审计与调参回查
        Index("ix_push_logs_runner_id_created_at", "runner_id", text("created_at DESC")),
        # 5.3：扩圈排除已推人群（BR-14）
        Index("ix_push_logs_order_id_round", "order_id", "round"),
        CheckConstraint(
            "channel IN ('INBOX', 'PUSH', 'HALL_ONLY')", name="channel_enum"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PushLog order_id={self.order_id} runner_id={self.runner_id} round={self.round}>"


class Notification(Base, CreatedAtMixin):
    """站内消息（5.2 notifications，必达主通道 FR-PUSH-06）。"""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.id", ondelete="CASCADE", name="fk_notifications_user_id_users"
        ),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="PUSH_NEW_ORDER/ACCEPTED/PROGRESS/DELIVERED/NEED_CONFIRM/...",
    )
    title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="深链参数 {order_id, tab}"
    )
    read_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="红点 = read_at IS NULL 计数"
    )

    __table_args__ = (
        # 5.3：站内信列表与红点计数
        Index(
            "ix_notifications_user_id_read_at_created_at",
            "user_id",
            "read_at",
            text("created_at DESC"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Notification user_id={self.user_id} kind={self.kind} read={self.read_at is not None}>"
