"""认证模块：注册 / 登录 / 令牌校验（T-07 简化版）。

## 本轮的简化边界（明确写下，避免被当成"已完成"）

| 项 | 本轮 | 后续 |
|---|---|---|
| 凭据 | 手机号 + 密码（bcrypt） | 学号邮箱验证码（BR-01）、微信登录 |
| 注册即生效 | `verify_status='APPROVED'` | 提交学号 → 审核 → APPROVED |
| 令牌 | JWT（HS256）+ `token_version` | 同左（无变化） |
| 刷新 | 无（`jwt_access_ttl_min` 到期重登） | refresh token |

**为什么注册直接给 `APPROVED`**：`guard_create` 要求 `actor.is_verified`，
而学号认证在 M1 明确不做 —— 若注册留 `UNVERIFIED`，新用户一单都发不出去
（403/40301），整条演示链路断在第一步。这里是"口径一致"而非"绕过校验"：
认证模块本轮的产出就是「一个可信的、已通过简化认证的大学生身份」。

## 为什么不复用 `users.phone_enc` 做登录查询

`phone_enc` 是 AES-GCM 密文、nonce 随机 ⇒ 同一手机号两次加密结果不同，
不能等值查询。登录查档走 `auth_identities(provider='PHONE',
provider_uid=sha256(phone))`，那条唯一索引在 0001 基线里已经存在 ——
本模块只是**正确地使用**它，不新增表、不新增列。
"""

from __future__ import annotations

from app.modules.auth.repo import PROVIDER_PHONE, AuthRepo
from app.modules.auth.service import AuthResult, AuthService

__all__ = ["PROVIDER_PHONE", "AuthRepo", "AuthResult", "AuthService"]
