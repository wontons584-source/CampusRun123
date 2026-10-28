"""FastAPI 依赖：当前用户（JWT）与请求级数据库会话。

## T-07 起这里是**真认证**

占位实现（`X-User-Id` 头）已删除。任何客户端只要自报 id 就能以他人身份操作，
那不是"临时简化"，是一个**任何人都能复现的越权漏洞** ——
现在只剩一条路径：`Authorization: Bearer <jwt>` → 校验签名/过期 →
按主键查库 → 比对 `token_version` 与账号状态。

## 事务为什么放在依赖里（`get_db_session`）

K-09 要求"一个请求一个事务"，而 service 层一律**不 commit**（否则
`apply()` 的「先 append 事实、再 CAS 聚合」会被拆成两次提交，
中途崩溃就会出现"事件写了、状态没改"的半截单）。
本依赖在 `yield` 之后提交、异常时回滚，正好把"整条请求"包成一个事务边界。

> 注意 `yield` 之后的提交**只在正常返回时执行**：路由抛 `ApiError` 时
> FastAPI 会把异常抛回生成器（在 `yield` 处），走 `except` 分支回滚 ——
> 所以 409/422 这类失败**不会**留下任何写入。这也是 `/orders/{id}/grab`
> 能够"抢单失败什么都不留"的前提。

## 为什么提交放在依赖而不是每个路由里

20 个路由各写一次 `db.commit()`，漏一个就是"接口 200 但数据没落库"。
实测这类 bug 在开发阶段极难发现（`/orders` 发单就曾是这样：
service 正确地不 commit，但路由也没 commit，于是订单静默丢失）。
收敛到一个地方，漏不掉。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.core.security import bearer_token
from app.models.user import User

logger = logging.getLogger("campusrun.deps")

#: `verify_status` 中代表"已通过认证"的唯一取值（BR-01）。
VERIFIED_STATUS: str = "APPROVED"


@dataclass(frozen=True)
class CurrentUser:
    """当前登录用户（JWT 解析 + 查库后的真实用户投影）。

    与 `app.models.user.User` 分开的理由：ORM 实体带着 `phone_enc` 与
    `password_hash`，一旦被顺手 `json.dumps` 或塞进日志就是安全事故。
    这里的字段是**白名单**，路由层拿不到任何密文。

    `is_verified` 是 `guard_create` / `guard_grab` 的入参，必须来自库里
    的 `verify_status`，**不能**由客户端头声明（旧占位实现正是这么错的）。
    """

    id: int
    is_verified: bool
    nickname: str = ""
    is_runner_mode: bool = False
    phone_mask: str | None = None
    verify_status: str = "UNVERIFIED"
    credit_score: Any = None
    status: str = "ACTIVE"

    @classmethod
    def from_user(cls, user: User) -> CurrentUser:
        """从 ORM 用户构造（**只取白名单字段**，密文一律不出现在这里）。"""
        return cls(
            id=int(user.id),
            is_verified=user.verify_status == VERIFIED_STATUS,
            nickname=str(user.nickname),
            is_runner_mode=bool(user.is_runner_mode),
            phone_mask=user.phone_mask,
            verify_status=str(user.verify_status),
            credit_score=user.credit_score,
            status=str(user.status),
        )

    def to_dict(self) -> dict[str, Any]:
        """`GET /api/v1/me` 的响应体（Decimal 由 `_jsonable` 转字符串）。"""
        return {
            "id": self.id,
            "nickname": self.nickname,
            "phone_mask": self.phone_mask,
            "verify_status": self.verify_status,
            "is_verified": self.is_verified,
            "is_runner_mode": self.is_runner_mode,
            "credit_score": self.credit_score,
            "status": self.status,
        }


def get_db_session() -> Iterator[Session]:
    """请求级数据库会话：**成功才提交，异常必回滚**（K-09）。

    懒导入 `SessionLocal`：本模块被 `app.main` 在 import 期引用，
    而 `app.db.session` 在 import 期会 `create_engine`（虽是惰性、不建连接，
    但保持"依赖模块不在 import 期碰数据层"这条纪律更省心）。
    """
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_current_user(
    db: Annotated[Session, Depends(get_db_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    """解析 `Authorization: Bearer <jwt>` 并返回真实用户。

    Args:
        db: 请求级会话（同一个事务里查用户 —— 与后续业务写入同事务，
            不会出现"读到旧状态"的跨事务不一致）。
        authorization: 原始请求头。

    Returns:
        通过签名、过期、`token_version`、账号状态四道校验的用户。

    Raises:
        ApiError: 401/40101 未带凭据、401/40102 过期、401/40103 无效、
            403/40302 封禁（详见 `AuthService.user_from_token`）。
    """
    from app.modules.auth.service import AuthService

    token = bearer_token(authorization)
    user = AuthService(db).user_from_token(token)
    return CurrentUser.from_user(user)


__all__ = ["VERIFIED_STATUS", "CurrentUser", "get_current_user", "get_db_session"]
