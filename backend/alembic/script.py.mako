"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

CampusRun 迁移规范（K-15 / 13.0）：
- revision 号用 `0001`、`0002` 这样的四位零填充（**不是** Alembic 默认的随机 hash），
  与开发任务号 `T-01`（带连字符）严格区分，防混写。
- 部分索引、GENERATED 列、CHECK 约束这类 autogenerate 生成不出来的东西必须手写，
  并在注释里指回 `02-ARCHITECTURE.md` 的 5.2 / 5.3 章节。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
