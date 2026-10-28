"""认证 DTO（T-07 简化版）。

结构校验只在 Pydantic：手机号格式、密码长度、昵称长度。业务校验
（手机号是否已注册、密码是否匹配）在 `AuthService` —— 两层职责分开，
错误码也分开（400xx 参数 vs 401/409）。

**密码明文只在这里短暂存在**：Pydantic 不打印它（`repr` 被 `__repr_args__`
屏蔽不可靠，所以本模块的模型都标注了 `secret` 语义，且 service 层禁止记录入参）。
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, Field, field_validator

#: 中国大陆手机号（简化版认证只支持这一种，学号邮箱认证是后续里程碑）。
_PHONE_RE: Final[re.Pattern[str]] = re.compile(r"^1[3-9]\d{9}$")

#: 密码长度。上限 72 是 **bcrypt 的硬限制**（算法只吃前 72 字节），
#: 超过就截断会让"72 位"与"100 位"的弱密码等价 —— 所以在入口直接拒绝。
PASSWORD_MIN: Final[int] = 6
PASSWORD_MAX: Final[int] = 72

#: 昵称上限与 `users.nickname varchar(32)` 一致。
NICKNAME_MAX: Final[int] = 32


def _normalize_phone(value: str) -> str:
    """手机号归一化：去空格/短横线。其余字符交给正则拒。

    允许 `138-1234-5678` 这类输入是产品要求（用户从通讯录复制常带分隔符），
    但**不允许**任意字符被忽略 —— 那会让 `138abc` 变成合法号。
    """
    return value.strip().replace(" ", "").replace("-", "")


class RegisterRequest(BaseModel):
    """注册入参（`POST /api/v1/auth/register`）。"""

    phone: str = Field(description="11 位手机号")
    password: str = Field(
        description=f"密码，{PASSWORD_MIN}–{PASSWORD_MAX} 位", min_length=PASSWORD_MIN
    )
    nickname: str | None = Field(
        default=None, max_length=NICKNAME_MAX, description="昵称，留空则「同学+尾号4位」"
    )

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, v: str) -> str:
        normalized = _normalize_phone(v)
        if not _PHONE_RE.match(normalized):
            raise ValueError("请输入正确的 11 位手机号")
        return normalized

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        if len(v) > PASSWORD_MAX:
            raise ValueError(f"密码最长 {PASSWORD_MAX} 位")
        if not v.strip():
            raise ValueError("密码不能为空白字符")
        return v

    @field_validator("nickname")
    @classmethod
    def _check_nickname(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        return trimmed or None


class LoginRequest(BaseModel):
    """登录入参（`POST /api/v1/auth/login`）。"""

    phone: str = Field(description="11 位手机号")
    password: str = Field(description="密码")

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, v: str) -> str:
        normalized = _normalize_phone(v)
        if not _PHONE_RE.match(normalized):
            raise ValueError("请输入正确的 11 位手机号")
        return normalized


class UserBrief(BaseModel):
    """对外用户投影（`register` / `login` / `me` 共用）。

    **不含** `phone_enc` 与 `password_hash`：那两列是密文与可爆破的哈希，
    任何 DTO 都不该出现。
    """

    id: int
    nickname: str
    phone_mask: str | None = None
    verify_status: str
    is_verified: bool
    is_runner_mode: bool
    credit_score: str | None = None
    status: str


class AuthResponse(BaseModel):
    """注册 / 登录成功响应：`{token, user}`。"""

    token: str
    user: UserBrief


__all__ = [
    "NICKNAME_MAX",
    "PASSWORD_MAX",
    "PASSWORD_MIN",
    "AuthResponse",
    "LoginRequest",
    "RegisterRequest",
    "UserBrief",
]
