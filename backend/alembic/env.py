"""Alembic 环境配置（T-03）。

**同时支持 offline 与 online 两种模式**，这是本任务唯一的可离线验证手段：

- `alembic upgrade head`（online）：连真实 PG 执行迁移。容器内的路径。
- `alembic upgrade head --sql`（offline）：**不建立任何连接**，只把 DDL 打到 stdout。
  开发机上没有数据库时用它核对「14 张表 + 全部索引 + 部分索引 WHERE 子句」。

约束（C1）：本文件在 import 期不做任何 I/O —— `create_engine` 是惰性的，
但 offline 分支连 `create_engine` 都不调用，只把 URL 字符串交给 context。

URL 来源：`app.core.config.settings.database_url`（alembic.ini 里留空），
这样「迁移跑到哪个库」与「应用连哪个库」永远同一处配置。
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from app.core.config import settings
from app.models import Base
from sqlalchemy import pool

# Alembic Config 对象：提供 alembic.ini 的访问入口
config = context.config

# 日志：有 alembic.ini 就按它配；被程序内调用（测试）时跳过
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # noqa: BLE001 - 程序内调用时 ini 可能存在但无 logger 段
        logging.getLogger("alembic").setLevel(logging.INFO)

# 目标 metadata：来自 app.models（__init__ 里已 import 全部 14 张表）
target_metadata = Base.metadata


def _database_url() -> str:
    """迁移目标库地址：ini 里显式写了就用 ini，否则用应用配置。"""
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url
    return settings.database_url


def run_migrations_offline() -> None:
    """Offline 模式：不连库，只生成 SQL。

    这是 `--sql` 的入口。`literal_binds=True` 让参数值直接内联进 SQL
    （否则会输出 `%(param)s` 占位符，人读不了也 diff 不了）。
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
        include_schemas=False,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Online 模式：建立连接后执行迁移。"""
    from sqlalchemy import create_engine

    connectable = create_engine(_database_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_schemas=False,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
