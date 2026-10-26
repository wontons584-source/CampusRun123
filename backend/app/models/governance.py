"""治理聚合根：sys_configs / admin_audit_logs。

- `sys_configs`（US-22 参数热更新）：业务阈值全部走 `config.get(key)`（K-11），
  改完 ≤60s 生效、不重启；每次改 `version+1` 并写审计。
- `admin_audit_logs`（D20 不可变）：**只 INSERT**，DB 层收回 UPDATE/DELETE 权限
  （权限收回写在 deploy 的初始化脚本里，不写在迁移里 —— 迁移只管结构）。
  `before` / `after` 存改前改后值，让「谁在什么时候把参数从天改到地」可回溯。
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin

AUDIT_TARGET_TYPES: tuple[str, ...] = ("ORDER", "USER", "POI", "CONFIG", "DISPUTE", "SYSTEM")

AUDIT_ACTIONS: tuple[str, ...] = (
    "FORCE_CLOSE",
    "BAN",
    "UNBAN",
    "VERIFY_PASS",
    "VERIFY_REJECT",
    "CREDIT_ADJUST",
    "CONFIG_UPDATE",
    "ARBITRATE_FREE",
    "ARBITRATE_LIABLE",
    "VIEW_PICK_CODE",
)


class SysConfig(Base):
    """运行期参数（5.2 sys_configs）。

    主键是业务 key（`push.radius_m` 等），不是自增 id —— 参数表按 key 点查，
    自增 id 只是多一次无用的间接层。
    没有 `created_at`：文档 5.2 只给了 `updated_by / updated_at / version`。
    """

    __tablename__ = "sys_configs"

    key: Mapped[str] = mapped_column(
        String(48), primary_key=True, comment="如 push.radius_m"
    )
    value: Mapped[Any] = mapped_column(JSONB, nullable=False, comment="类型化读取")
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="操作管理员 users.id（不加 FK，见 5.2）"
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="UTC",
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
        comment="每次改 +1，写审计",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SysConfig key={self.key} version={self.version}>"


class AdminAuditLog(Base, CreatedAtMixin):
    """管理操作审计（5.2 admin_audit_logs，D20 不可变）。"""

    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.id", ondelete="RESTRICT", name="fk_admin_audit_logs_admin_id_users"
        ),
        nullable=False,
    )
    target_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="ORDER/USER/POI/CONFIG/DISPUTE"
    )
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="FORCE_CLOSE/BAN/UNBAN/VERIFY_PASS/VERIFY_REJECT/CREDIT_ADJUST/"
        "CONFIG_UPDATE/ARBITRATE_FREE/ARBITRATE_LIABLE/VIEW_PICK_CODE",
    )
    before: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="改前值"
    )
    after: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="改后值"
    )
    note: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="强制关闭必填原因（BR-32）"
    )

    __table_args__ = (
        # 5.3：追溯（D20）
        Index(
            "ix_admin_audit_logs_target_type_target_id_created_at",
            "target_type",
            "target_id",
            text("created_at DESC"),
        ),
        Index(
            "ix_admin_audit_logs_admin_id_created_at",
            "admin_id",
            text("created_at DESC"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AdminAuditLog admin_id={self.admin_id} action={self.action}>"
