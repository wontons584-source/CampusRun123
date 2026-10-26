"""数据库会话管理。

设计要点：
- 同步 SQLAlchemy 2.0（FastAPI 用线程池跑同步 ORM 足够，本项目 QPS 峰值 ~100，
  异步驱动带来的复杂度不划算；需要并发的地方用 Celery 与 Redis 扛）。
- 事务边界在 service 层显式控制：`with session.begin()`。中途抛错整段回滚，
  避免出现"状态改了、进度事件没写"的半截数据（状态机的核心约束）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_pool_size,
    pool_pre_ping=True,          # 连接被中间件掐断后自动重建
    echo=settings.db_echo,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：请求级会话。不在这里 commit，提交由 service 决定。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def transaction() -> Iterator[Session]:
    """显式事务。异常即回滚，正常结束才提交。"""
    db = SessionLocal()
    try:
        with db.begin():
            yield db
    finally:
        db.close()


def ping_db() -> bool:
    """健康检查用：只验证连接可用，不做业务查询。"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - 健康检查不应该因任何异常把进程带崩
        return False
