"""add users.password_hash

Revision ID: 0003
Revises: 0002
Create Date: 2025-01-01 00:00:02.000000

T-07（简化版认证）：`users` 增加 `password_hash`。

## 为什么是**新增迁移**而不是改 0001

0001 是基线（14 张表 + 27 条索引），已经在测试库与任何演示环境跑过。
改已发布的迁移会让「已经 upgrade 过的库」与「重新 upgrade 的库」结构不一致，
而且 `alembic_version` 表无法表达"同一个 revision 有两种结构"——
这类漂移只能靠人工比对，是迁移体系里最贵的一类故障。
故 0003 只做一件事：`ADD COLUMN`。

## 为什么可空（`nullable=True`）

1. 存量用户（0001/0002 期间建的）没有密码，非空加列会在有数据的库上直接失败；
2. 语义上「没有密码」是合法的：只绑定了微信的用户没有密码；
3. 登录判空由 service 层转成 `401/40103`（令牌/凭据无效），不是 DB 约束错误。

## 为什么不建唯一索引

手机号明文**不入库**（`phone_enc` 是 AES-GCM 密文，每次 nonce 随机 ⇒ 同一手机号
两次加密的密文不同），所以 `phone_enc` 上没有可用的唯一约束。
手机号唯一性由 `auth_identities(provider='PHONE', provider_uid=sha256(phone))`
的唯一索引承担 —— 那条索引 0001 里已经有了，本迁移不重复建。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """`users.password_hash`（可空，128 字符）。"""
    op.add_column(
        "users",
        sa.Column(
            "password_hash",
            sa.String(length=128),
            nullable=True,
            comment="bcrypt 哈希（含盐）；NULL = 未设置密码，只能走第三方登录。禁止落日志",
        ),
    )


def downgrade() -> None:
    """回滚只删这一列；`users` 其余列与索引不受影响。"""
    op.drop_column("users", "password_hash")
