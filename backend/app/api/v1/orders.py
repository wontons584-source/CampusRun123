"""订单 API（T-13 发单；T-14 详情后续扩展）。

`POST /orders`：发单。业务校验在 `OrderService.create_order`，
本层只做 DTO 绑定 + 事务收口 + 响应包装。
"""

from __future__ import annotations

from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user, get_db_session
from app.core.response import ok
from app.models.order import Order
from app.modules.order.service import OrderService
from app.schemas.order import CreateOrderRequest

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("", summary="发布订单（T13）")
def create_order(
    body: CreateOrderRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """发单。service 负责校验与落库，本层只包装响应。"""
    service = OrderService(db)
    order_id = service.create_order(
        publisher_id=user.id,
        is_verified=user.is_verified,
        type_=body.type,
        from_poi_id=body.from_poi_id,
        to_poi_id=body.to_poi_id,
        reward=body.reward,
        expect_minutes=body.expect_minutes,
        description=body.description,
        images=tuple(body.images),
        pick_code=body.pick_code,
    )
    order_no = str(
        db.execute(sa.select(Order.order_no).where(Order.id == order_id)).scalar_one()
    )
    data: dict[str, Any] = {"id": order_id, "order_no": order_no, "status": "PENDING"}
    return JSONResponse(status_code=201, content=ok(data, "订单发布成功"))


__all__ = ["router"]
