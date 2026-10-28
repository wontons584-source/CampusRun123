"""认证服务（T-07 简化版）：注册 / 登录 / 令牌校验。

## 事务边界（K-09）

service 层**不 commit**。一次注册 = `users` 一行 + `auth_identities` 一行，
两行必须同生共死；提交由 api 层的 `get_db_session` 依赖在请求结束时统一做。
中途抛错 → 依赖里 `rollback` → 不会留下"有用户没身份"的孤儿账号。

## 登录为什么不能告诉用户"手机号不存在"

`手机号未注册` 与 `密码错误` 必须是**同一个** `401/40103`：
一旦区分，登录接口就成了手机号枚举器（攻击者能批量确认哪些号码是本站用户）。
文案统一为「手机号或密码不正确」，日志里才区分（便于客服排障）。
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.orm import Session

from app.core.errors import ApiError, ErrCode
from app.core.security import (
    create_access_token,
    decode_token,
    token_subject,
    token_version_of,
    verify_password,
)
from app.models.user import User
from app.modules.auth.repo import (
    STATUS_ACTIVE,
    STATUS_BANNED,
    STATUS_DELETED,
    AuthRepo,
)

logger = logging.getLogger("campusrun.auth")

#: 登录失败的统一文案（不区分"未注册"与"密码错"，见模块文档）。
LOGIN_FAILED_MESSAGE: Final[str] = "手机号或密码不正确"

#: `is_verified` 的唯一判据（BR-01）。`VERIFY_APPROVED` 之外一律不算认证通过。
VERIFIED_STATUS: Final[str] = "APPROVED"


@dataclass(frozen=True, slots=True)
class AuthResult:
    """一次成功认证的产物：令牌 + 用户。"""

    token: str
    user: User


def user_public(user: User) -> dict[str, Any]:
    """用户对外投影（`register` / `login` / `GET /me` 共用）。

    **绝不包含** `phone_enc`（密文）与 `password_hash`：前者一次泄露等于批量
    手机号失守，后者虽然不可逆但仍可离线爆破。只回 `phone_mask`。

    金额/时间统一交给 `app.core.response._jsonable` 处理（Decimal → str、
    datetime → ISO8601 Z），这里只负责"给哪些字段"。
    """
    return {
        "id": int(user.id),
        "nickname": user.nickname,
        "phone_mask": user.phone_mask,
        "avatar_url": user.avatar_url,
        "verify_status": user.verify_status,
        "is_verified": user.verify_status == VERIFIED_STATUS,
        "is_runner_mode": bool(user.is_runner_mode),
        "credit_score": user.credit_score,
        "credit_level": user.credit_level,
        "status": user.status,
    }


class AuthService:
    """认证领域入口。"""

    __slots__ = ("_db", "_repo")

    def __init__(self, db: Session) -> None:
        self._db = db
        self._repo = AuthRepo(db)

    # ---------------------------------------------------------------- 注册
    def register(
        self,
        *,
        phone: str,
        password: str,
        nickname: str | None = None,
    ) -> AuthResult:
        """注册并直接签发令牌（注册即登录，少一次往返）。

        Args:
            phone: 11 位手机号明文（Pydantic 已校验格式）。
            password: 明文密码（Pydantic 已校验 6–72 位）。
            nickname: 可选昵称；空则「同学+尾号4位」。

        Returns:
            `AuthResult(token, user)`。

        Raises:
            ApiError: 409/40900 手机号已注册。
        """
        user = self._repo.create_user_with_phone(phone=phone, password=password, nickname=nickname)
        token = create_access_token(int(user.id), token_version=int(user.token_version))
        logger.info("auth.register user_id=%s", user.id)
        return AuthResult(token=token, user=user)

    # ---------------------------------------------------------------- 登录
    def login(self, *, phone: str, password: str, now: datetime.datetime | None = None) -> AuthResult:
        """手机号 + 密码登录。

        Args:
            phone: 手机号明文。
            password: 明文密码。
            now: 签发时刻（测试注入固定时钟）。

        Returns:
            `AuthResult(token, user)`。

        Raises:
            ApiError: 401/40103 凭据无效；403/40302 账号被封禁。
        """
        user = self._repo.user_by_phone(phone)
        if user is None or not verify_password(password, user.password_hash):
            # 日志里区分（客服排障用），响应里不区分（防手机号枚举）
            logger.info("auth.login_failed phone_registered=%s", user is not None)
            raise ApiError(401, ErrCode.TOKEN_INVALID, LOGIN_FAILED_MESSAGE)
        self._ensure_usable(user)
        token = create_access_token(
            int(user.id), token_version=int(user.token_version), now=now
        )
        logger.info("auth.login user_id=%s", user.id)
        return AuthResult(token=token, user=user)

    # ------------------------------------------------------------ 令牌校验
    def user_from_token(self, token: str) -> User:
        """`Authorization: Bearer <token>` → 真实用户行。

        校验链（顺序即失败优先级）：
        1. 签名 / 过期 → `decode_token` 抛 401（40102 过期 / 40103 非法）；
        2. 用户不存在（注销后令牌还在）→ 401/40103，让客户端清掉本地令牌；
        3. `token_version` 不一致 → 401/40103（ADR-05：改密码/封号后旧令牌立刻失效）；
        4. 账号状态非 ACTIVE → 403/40302（封禁）或 401（已删除）。

        Args:
            token: 裸 JWT（不含 `Bearer` 前缀）。

        Returns:
            已通过全部校验的 `User`。
        """
        payload = decode_token(token)
        user_id = token_subject(payload)
        user = self._repo.user_by_id(user_id)
        if user is None:
            raise ApiError(401, ErrCode.TOKEN_INVALID, "账号不存在，请重新登录")
        if int(user.token_version) != token_version_of(payload):
            raise ApiError(401, ErrCode.TOKEN_INVALID, "登录状态已失效，请重新登录")
        self._ensure_usable(user)
        return user

    # ---------------------------------------------------------------- 内部
    def _ensure_usable(self, user: User) -> None:
        """账号可用性：`ACTIVE` 之外一律拒绝。

        封禁用 `403/40302`（用户能理解"我被封了、去申诉"），
        删除用 `401/40103`（对客户端等价于"这个令牌没有主体了"）。
        """
        status = str(user.status)
        if status == STATUS_ACTIVE:
            return
        if status == STATUS_BANNED:
            raise ApiError(403, ErrCode.ACCOUNT_BANNED, "账号已被封禁，请联系管理员")
        if status == STATUS_DELETED:
            raise ApiError(401, ErrCode.TOKEN_INVALID, "账号已注销")
        # 未知状态（脏数据）：按封禁处理，不放行
        raise ApiError(403, ErrCode.ACCOUNT_BANNED, "账号状态异常，请联系管理员")


__all__ = ["LOGIN_FAILED_MESSAGE", "VERIFIED_STATUS", "AuthResult", "AuthService", "user_public"]
