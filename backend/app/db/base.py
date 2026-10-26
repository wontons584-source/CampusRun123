"""ORM 基类与通用 Mixin。

约定（对应 02-ARCHITECTURE 第 13 章）：
- 表名 snake_case 复数；模型类 PascalCase 单数。
- 时间统一 timestamptz + UTC，禁止 naive datetime。
- 金额一律 Numeric(6,2) ↔ Decimal，禁止 float。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 显式命名约束，保证 Alembic 自动生成的迁移稳定（否则 PostgreSQL 会给匿名约束
# 起随机名，删改时对不上）。
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """创建/更新时间。数据库侧默认值，避免应用忘了写。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CreatedAtMixin:
    """append-only 表只需要 created_at，不要 updated_at（有 updated_at 就是在暗示可改）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def gen_order_no() -> str:
    """人读订单号前缀：CR + yyMMdd；6 位随机段由 repo 层补齐并保证唯一。"""
    return "CR" + datetime.now().strftime("%y%m%d")


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
