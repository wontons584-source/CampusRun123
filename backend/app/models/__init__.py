"""ORM 模型包 —— Alembic 收集 metadata 的**唯一入口**。

`alembic/env.py` 只 import 本模块的 `Base`，所以这里必须把全部模型文件 import 一遍；
漏一个文件的后果是「迁移里少一张表」，而 `alembic check` 在没连库时看不出来。

14 张表（5.2，一张不能多、一张不能少）：
    users / auth_identities / student_verifications / pois / orders /
    order_progress / order_cancel_requests / reviews / credit_logs / disputes /
    push_logs / notifications / sys_configs / admin_audit_logs

注意：本模块**只做导入与再导出**，不允许出现任何 I/O（C1：无库无 Redis 也能 import）。
"""

from __future__ import annotations

from app.db.base import Base
from app.models.dispute import Dispute
from app.models.governance import AdminAuditLog, SysConfig
from app.models.order import Order, OrderCancelRequest, OrderProgress
from app.models.poi import Poi
from app.models.push import Notification, PushLog
from app.models.review import CreditLog, Review
from app.models.user import AuthIdentity, StudentVerification, User

__all__ = [
    "Base",
    "AdminAuditLog",
    "AuthIdentity",
    "CreditLog",
    "Dispute",
    "Notification",
    "Order",
    "OrderCancelRequest",
    "OrderProgress",
    "Poi",
    "PushLog",
    "Review",
    "StudentVerification",
    "SysConfig",
    "User",
]
