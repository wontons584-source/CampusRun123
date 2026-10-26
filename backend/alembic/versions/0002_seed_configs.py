"""seed configs

Revision ID: 0002
Revises: 0001
Create Date: 2025-01-01 00:00:01.000000

T-06：参数化配置的种子数据。把 `ConfigService.DEFAULTS` 的每个键写进
`sys_configs` 表（`version=1`），这样后台第一次打开参数页就有东西可改，
业务代码 `cfg(key)` 的回源路径也能命中。

幂等：用 `INSERT ... ON CONFLICT (key) DO NOTHING`，重复跑不炸、不覆盖
运营已经改过的值（ON CONFLICT 不动 version）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.modules.admin.configs import DEFAULTS

#: revision 标识，供 `alembic downgrade` 使用
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """幂等种子：`ON CONFLICT (key) DO NOTHING`，不覆盖运营已改的值。"""
    bind = op.get_bind()
    for key, value in DEFAULTS.items():
        bind.execute(
            sa.text(
                "INSERT INTO sys_configs (key, value, version) "
                "VALUES (:key, CAST(:value AS jsonb), 1) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"key": key, "value": json.dumps(value, ensure_ascii=False)},
        )


def downgrade() -> None:
    """回滚只删种子键（不动运营后来自己加的参数）。"""
    bind = op.get_bind()
    bind.execute(
        sa.text("DELETE FROM sys_configs WHERE key = ANY(:keys)"),
        {"keys": list(DEFAULTS.keys())},
    )
