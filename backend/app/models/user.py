"""用户聚合根：users / auth_identities / student_verifications。

对应 02-ARCHITECTURE 5.2（表结构）与 5.3（索引清单）。

设计要点：
- `users.credit_level` 是 **GENERATED 列**（BR-26），由 `credit_score` 派生，
  不允许应用层写入 —— 任何 `UPDATE users SET credit_level=...` 都是 bug。
  用生成列而不是触发器/应用层计算，是为了让「分数改了等级没改」这类脏数据
  在数据库层面不可能出现。
- 登录身份拆到 `auth_identities`（US-01）：同一用户可绑手机号 + 微信，
  `(provider, provider_uid)` 唯一保证「同一通道不重复建档」。
- 手机号明文永不入库：`phone_enc` 存 AES-GCM 密文，`phone_mask` 存
  `138****8888` 直接供接口返回（D16 脱敏展示），列表接口只读后者。
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin

# BR-26 信用等级分档：优秀 ≥110 / 良好 95–109 / 正常 80–94 / 受限 <80。
# 这段 SQL 是 credit_level 的**唯一**定义点，Alembic 迁移 0001 里逐字复制。
CREDIT_LEVEL_EXPR: str = (
    "CASE "
    "WHEN credit_score >= 110 THEN 'EXCELLENT' "
    "WHEN credit_score >= 95 THEN 'GOOD' "
    "WHEN credit_score >= 80 THEN 'NORMAL' "
    "ELSE 'LIMITED' "
    "END"
)

# 状态枚举的取值（K-03）。用 varchar + 注释承载，不建 PG ENUM：
# 后续加值（M2 增加「已撤回」等）只需改应用层，不必 `ALTER TYPE` 锁表。
VERIFY_STATUSES: tuple[str, ...] = ("UNVERIFIED", "REVIEWING", "APPROVED", "REJECTED")
USER_STATUSES: tuple[str, ...] = ("ACTIVE", "BANNED", "DELETED")
AUTH_PROVIDERS: tuple[str, ...] = ("PHONE", "WECHAT")
VERIFY_METHODS: tuple[str, ...] = ("EMAIL", "OCR")
VERIFY_RECORD_STATUSES: tuple[str, ...] = ("PENDING", "APPROVED", "REJECTED")


class User(Base, TimestampMixin):
    """用户主表（5.2 users）。

    注意 `student_no` 可空：未认证用户没有学号（BR-02 只要求「有则全局唯一」）。
    用 PostgreSQL 的默认语义（NULL 互不相等）即可满足「多个未认证用户共存」，
    不要加 `NULLS NOT DISTINCT` —— 那会让第二个未认证用户插不进去。
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    student_no: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="BR-02 全局唯一；未认证为 NULL")
    nickname: Mapped[str] = mapped_column(String(32), nullable=False, comment="默认「同学+尾号4位」")
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    phone_enc: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True, comment="AES-GCM 密文，禁止进任何 DTO")
    phone_mask: Mapped[str | None] = mapped_column(String(11), nullable=True, comment="138****8888，直接供接口返回")
    dept: Mapped[str | None] = mapped_column(String(64), nullable=True)
    grade: Mapped[str | None] = mapped_column(String(8), nullable=True)
    verify_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        server_default=text("'UNVERIFIED'"),
        comment="UNVERIFIED/REVIEWING/APPROVED/REJECTED（FR-AUTH-04）",
    )
    credit_score: Mapped[Any] = mapped_column(
        Numeric(5, 2),
        nullable=False,
        server_default=text("100"),
        comment="BR-25 区间 0–120",
    )
    credit_level: Mapped[str] = mapped_column(
        String(8),
        Computed(CREDIT_LEVEL_EXPR, persisted=True),
        nullable=False,
        comment="GENERATED：EXCELLENT/GOOD/NORMAL/LIMITED（BR-26），禁止应用层写入",
    )
    status: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        server_default=text("'ACTIVE'"),
        comment="ACTIVE/BANNED/DELETED",
    )
    token_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="ADR-05：+1 即全部旧 token 失效",
    )
    is_runner_mode: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        comment="FR-ME-03 召回硬条件",
    )
    notify_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        comment="推送开关/免打扰时段",
    )

    __table_args__ = (
        # 5.3：学号唯一校验（BR-02）、后台按学号检索
        Index("uq_users_student_no", "student_no", unique=True),
        # 5.3：后台认证待审队列（FR-ADMIN-02）
        Index("ix_users_verify_status_created_at", "verify_status", "created_at"),
        CheckConstraint(
            "credit_score >= 0 AND credit_score <= 120",
            name="credit_score_range",
        ),
        CheckConstraint(
            "verify_status IN ('UNVERIFIED', 'REVIEWING', 'APPROVED', 'REJECTED')",
            name="verify_status_enum",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'BANNED', 'DELETED')",
            name="status_enum",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"<User id={self.id} nickname={self.nickname!r} verify={self.verify_status}>"


class AuthIdentity(Base, CreatedAtMixin):
    """登录身份（5.2 auth_identities）。

    与 users 分离以支持多通道：US-01 用 `(provider, provider_uid)` 查档，
    查到即登录、查不到才建档，避免「手机号登录」与「微信登录」互相覆盖。
    """

    __tablename__ = "auth_identities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_auth_identities_user_id_users"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(12), nullable=False, comment="PHONE / WECHAT")
    provider_uid: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="手机号哈希 / openid"
    )

    __table_args__ = (
        # 5.3：登录查档（US-01）
        Index(
            "uq_auth_identities_provider_provider_uid",
            "provider",
            "provider_uid",
            unique=True,
        ),
        CheckConstraint("provider IN ('PHONE', 'WECHAT')", name="provider_enum"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuthIdentity user_id={self.user_id} provider={self.provider}>"


class StudentVerification(Base, CreatedAtMixin):
    """学号认证记录（5.2 student_verifications）。

    一用户可有多条（被打回后重新提交），取最新一条即为当前状态（US-03）。
    审核通过后 30 天删原图（BR-05）：`evidence_key` 由清理任务按 `created_at` 扫。
    """

    __tablename__ = "student_verifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_student_verifications_user_id_users"),
        nullable=False,
    )
    method: Mapped[str] = mapped_column(String(12), nullable=False, comment="EMAIL / OCR")
    student_no_input: Mapped[str | None] = mapped_column(String(20), nullable=True)
    name_input: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="认证邮箱")
    evidence_key: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="对象存储 key；BR-05 审核通过后 30 天删原图"
    )
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, comment="PENDING/APPROVED/REJECTED"
    )
    reject_reason: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="US-03 必须给具体原因"
    )
    reviewer_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="审核管理员 users.id（不加 FK，避免与删除流程耦合）"
    )
    reviewed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # 5.3：查最新一次认证状态
        Index("ix_student_verifications_user_id_created_at", "user_id", text("created_at DESC")),
        # 5.3：审核队列
        Index("ix_student_verifications_status_created_at", "status", "created_at"),
        CheckConstraint("method IN ('EMAIL', 'OCR')", name="method_enum"),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED')", name="status_enum"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StudentVerification user_id={self.user_id} status={self.status}>"
