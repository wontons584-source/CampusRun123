"""认证仓储：`users` / `auth_identities` 的读写（T-07）。

## 三层写入顺序是刻意的

`users` 先落、拿到自增 id，再写 `auth_identities`。反过来做不到（需要 user_id），
一次性 `INSERT ... RETURNING` 也做不到（两张表）。两步都在**同一个调用方事务**里，
所以"用户建了、身份没绑"这种半截数据不会提交出去 —— 仓储层**不 commit**（K-09）。

## 手机号唯一性靠数据库，不靠应用层 SELECT

`先 SELECT 查重再 INSERT` 在并发下必然漏（两个请求同时查到空 → 同时插入）。
唯一性由 `uq_auth_identities_provider_provider_uid` 保证，应用层只负责把
`IntegrityError` 翻译成 `409/40900`（而不是 500）。
这里仍保留一次 `user_by_phone` 预检：它不是为了"防并发"，而是为了给
99% 的正常重复注册一个**更快、更准**的错误路径（不用等唯一约束）。
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ApiError, ErrCode
from app.core.security import encrypt_phone, hash_password, mask_phone, phone_digest
from app.models.user import AuthIdentity, User
from app.modules.order.repo import map_integrity_error

#: 认证通道（US-01）。M1 只用 PHONE；WECHAT 预留在 `auth_identities` 的 CHECK 里。
PROVIDER_PHONE: Final[str] = "PHONE"

#: 简化认证下注册即为已认证（见模块文档的表）。
VERIFY_APPROVED: Final[str] = "APPROVED"

#: 昵称默认值前缀（BR-03：默认「同学+手机尾号4位」）。
DEFAULT_NICKNAME_PREFIX: Final[str] = "同学"

#: 用户状态（与 `users.status` 的 CHECK 一致）。
STATUS_ACTIVE: Final[str] = "ACTIVE"
STATUS_BANNED: Final[str] = "BANNED"
STATUS_DELETED: Final[str] = "DELETED"


def default_nickname(phone: str) -> str:
    """默认昵称：`同学` + 手机尾号 4 位（BR-03）。"""
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"{DEFAULT_NICKNAME_PREFIX}{digits[-4:]}" if len(digits) >= 4 else DEFAULT_NICKNAME_PREFIX


class AuthRepo:
    """`users` / `auth_identities` 的最小读写集合。

    构造即绑定一个 `Session`（不 commit、不开事务）—— 事务归 api 层的
    `get_db_session` 依赖统一收口。
    """

    __slots__ = ("_db",)

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------ 读
    def user_by_id(self, user_id: int) -> User | None:
        """按主键点查用户（每次鉴权都会走这条路，必须有主键索引）。"""
        return self._db.get(User, user_id)

    def user_by_phone(self, phone: str) -> User | None:
        """按手机号查用户：走 `auth_identities` 的确定性摘要 + 唯一索引。

        返回 `None` 表示"该手机号未注册"—— 调用方（登录）必须把它和
        "密码错了"合并成同一个响应，否则接口变成手机号枚举器。
        """
        stmt = (
            select(User)
            .join(AuthIdentity, AuthIdentity.user_id == User.id)
            .where(
                AuthIdentity.provider == PROVIDER_PHONE,
                AuthIdentity.provider_uid == phone_digest(phone),
            )
        )
        return self._db.execute(stmt).scalars().first()

    # ------------------------------------------------------------------ 写
    def create_user_with_phone(
        self,
        *,
        phone: str,
        password: str,
        nickname: str | None = None,
    ) -> User:
        """建用户 + 绑定手机号身份（同事务两步，返回 flush 后的 `User`）。

        Args:
            phone: 11 位手机号明文；落库前转成 `phone_enc`（密文）与 `phone_mask`（脱敏）。
            password: 明文密码，落库前 bcrypt。
            nickname: 为空则用「同学+尾号4位」。

        Returns:
            已 flush（id 可用）的 `User` 实例。

        Raises:
            ApiError: 409/40900 手机号已注册；其余 `IntegrityError` 按约束名映射。
        """
        if self.user_by_phone(phone) is not None:
            raise ApiError(409, ErrCode.RESOURCE_CONFLICT, "该手机号已注册，请直接登录")

        user = User(
            nickname=nickname or default_nickname(phone),
            phone_enc=encrypt_phone(phone),
            phone_mask=mask_phone(phone),
            verify_status=VERIFY_APPROVED,
            password_hash=hash_password(password),
            status=STATUS_ACTIVE,
        )
        self._db.add(user)
        try:
            self._db.flush()          # 拿到自增 id
            self._db.add(
                AuthIdentity(
                    user_id=user.id,
                    provider=PROVIDER_PHONE,
                    provider_uid=phone_digest(phone),
                )
            )
            self._db.flush()
        except IntegrityError as exc:
            # 并发注册同一手机号时预检会漏，唯一索引是最终防线。
            # 复用订单模块的映射表：约束名 → 业务码只有一份（K-02），不在这里另写一套。
            raise map_integrity_error(exc, default_message="该手机号已注册") from exc
        return user


__all__ = [
    "DEFAULT_NICKNAME_PREFIX",
    "PROVIDER_PHONE",
    "STATUS_ACTIVE",
    "STATUS_BANNED",
    "STATUS_DELETED",
    "VERIFY_APPROVED",
    "AuthRepo",
    "default_nickname",
]
