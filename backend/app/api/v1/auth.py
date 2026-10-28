"""认证 API（T-07 简化版）：注册 / 登录 / 当前用户。

三个端点都**薄**：DTO 绑定 → service → 统一响应包装。
业务判定（手机号重复、密码不匹配、账号封禁）全部在 `AuthService`，
本层不写任何 if/else 分支 —— 否则同一个规则会有两份实现。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user, get_db_session
from app.core.response import ok
from app.modules.auth.service import AuthService, user_public
from app.schemas.auth import LoginRequest, RegisterRequest

router = APIRouter(prefix="/auth", tags=["auth"])

#: `GET /me` 不带 `/auth` 前缀（FR-ME-01 把它放在用户中心根上）。
me_router = APIRouter(tags=["auth"])


def _auth_payload(token: str, user: Any) -> dict[str, Any]:
    """注册/登录的统一响应体：`{token, user}`。"""
    return {"token": token, "user": user_public(user)}


@router.post("/register", summary="注册（手机号 + 密码）", status_code=201)
def register(
    body: RegisterRequest,
    db: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """注册并直接签发令牌（注册即登录）。

    `verify_status` 为 `APPROVED`（简化认证，见 `app/modules/auth/__init__.py`）。
    """
    result = AuthService(db).register(
        phone=body.phone,
        password=body.password,
        nickname=body.nickname,
    )
    return JSONResponse(
        status_code=201,
        content=ok(_auth_payload(result.token, result.user), "注册成功"),
    )


@router.post("/login", summary="登录（手机号 + 密码）")
def login(
    body: LoginRequest,
    db: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """登录。凭据错误统一 `401/40103`（不区分"未注册"与"密码错"）。"""
    result = AuthService(db).login(phone=body.phone, password=body.password)
    return JSONResponse(
        status_code=200,
        content=ok(_auth_payload(result.token, result.user), "登录成功"),
    )


@me_router.get("/me", summary="当前登录用户")
def me(user: Annotated[CurrentUser, Depends(get_current_user)]) -> JSONResponse:
    """当前用户（白名单字段；**不含** `phone_enc` / `password_hash`）。"""
    return JSONResponse(status_code=200, content=ok(user.to_dict()))


__all__ = ["me_router", "router"]
