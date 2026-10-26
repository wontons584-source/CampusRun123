"""FastAPI 依赖（T-08 权限依赖的骨架；T-07 认证落地后替换占位实现）。

⚠️ **当前是占位认证**：`get_current_user` 从 `X-User-Id` / `X-User-Verified` 请求头
读身份，**只用于 T-13 发单接口在认证模块（T-07）完成前跑通链路**。
T-07 交付后必须替换为真正的 JWT 校验（`Authorization: Bearer <token>`），
并接入 `token_version` 即时失效（ADR-05）。占位实现刻意写得显眼，
任何把占位当正式实现的用法都会在 code review 里被一眼看穿。
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header

from app.core.errors import ApiError, ErrCode


@dataclass(frozen=True)
class CurrentUser:
    """当前登录用户（占位：T-07 换成 JWT 解析后的真实用户）。"""

    id: int
    is_verified: bool


def get_current_user(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_user_verified: str | None = Header(default=None, alias="X-User-Verified"),
) -> CurrentUser:
    """占位认证：读 `X-User-Id` 头。

    T-07 之后这里改为解析 `Authorization: Bearer` + 校验 `token_version`。
    """
    if not x_user_id:
        raise ApiError(401, ErrCode.UNAUTHENTICATED, "请先登录")
    try:
        user_id = int(x_user_id)
    except ValueError:
        raise ApiError(401, ErrCode.TOKEN_INVALID, "无效的身份标识") from None
    return CurrentUser(
        id=user_id,
        is_verified=x_user_verified in ("1", "true", "True"),
    )


def get_db_session():
    """请求级数据库会话（FastAPI 依赖）。"""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


__all__ = ["CurrentUser", "get_current_user", "get_db_session"]
