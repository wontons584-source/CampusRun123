"""评价与信誉聚合根：reviews / credit_logs。

- `reviews`：BR-27 一单一评价人一次 → `UNIQUE(order_id, from_id)`。
  评价窗口 7 天从 `completed_at` 起算（K-12：冻结期间评不了，回滚后应重新给 7 天），
  窗口校验在服务层（BR-24），DB 只保证唯一。
- `credit_logs`：只追加的流水账，`score_after` 冗余存一份，便于回溯
  「当时为什么是这个分」。信誉分本身在 `users.credit_score`，等级由生成列派生（BR-26）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin

REVIEW_DIRECTIONS: tuple[str, ...] = ("P2R", "R2P")

CREDIT_REASON_CODES: tuple[str, ...] = (
    "ORDER_DONE",
    "GOOD_REVIEW",
    "LATE",
    "ACCEPT_CANCEL",
    "DISPUTE_LOST",
    "ADMIN_ADJUST",
)


class Review(Base, CreatedAtMixin):
    """双向评价（5.2 reviews）。"""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE", name="fk_reviews_order_id_orders"),
        nullable=False,
    )
    from_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_reviews_from_id_users"),
        nullable=False,
    )
    to_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_reviews_to_id_users"),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(
        String(10), nullable=False, comment="P2R / R2P"
    )
    rating: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="1..5；BR-27 一单一评价人一次"
    )
    tags: Mapped[list[Any] | None] = mapped_column(
        JSONB, nullable=True, comment="预设标签（FR-REVIEW-03）"
    )
    content: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_hidden: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="隐私内容/冻结隐藏（BR-29）",
    )

    __table_args__ = (
        # 5.3：BR-27
        Index("uq_reviews_order_id_from_id", "order_id", "from_id", unique=True),
        # 5.3：个人主页近 20 条（FR-REVIEW-06）
        Index("ix_reviews_to_id_created_at", "to_id", text("created_at DESC")),
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),
        CheckConstraint("direction IN ('P2R', 'R2P')", name="direction_enum"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Review order_id={self.order_id} from={self.from_id} rating={self.rating}>"


class CreditLog(Base, CreatedAtMixin):
    """信誉流水（5.2 credit_logs）。

    `delta` 与 `score_after` 都是 `Numeric(5,2)`（BR-25 区间 0–120 用不上整数位更多），
    一律 Decimal，禁止 float —— 累加 0.5 分用 float 会出现 99.49999 这类脏值。
    """

    __tablename__ = "credit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_credit_logs_user_id_users"),
        nullable=False,
    )
    delta: Mapped[Any] = mapped_column(Numeric(5, 2), nullable=False, comment="正负均可")
    score_after: Mapped[Any] = mapped_column(
        Numeric(5, 2), nullable=False, comment="变更后分值，供回溯"
    )
    reason_code: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="ORDER_DONE/GOOD_REVIEW/LATE/ACCEPT_CANCEL/DISPUTE_LOST/ADMIN_ADJUST",
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="SET NULL", name="fk_credit_logs_order_id_orders"),
        nullable=True,
    )
    operator_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="管理员调整时的 users.id（不加 FK，见 5.2）"
    )
    note: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="管理员调整必填理由（US-21）"
    )

    __table_args__ = (
        # 5.3：信誉明细
        Index("ix_credit_logs_user_id_created_at", "user_id", text("created_at DESC")),
        CheckConstraint(
            "reason_code IN ('ORDER_DONE', 'GOOD_REVIEW', 'LATE', 'ACCEPT_CANCEL', "
            "'DISPUTE_LOST', 'ADMIN_ADJUST')",
            name="reason_code_enum",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CreditLog user_id={self.user_id} delta={self.delta} reason={self.reason_code}>"
