"""订单 API：发单 / 大厅 / 详情 / 抢单 / 履约进度 / 确认收货。

## 本层只做四件事

DTO 绑定 → 调 service → 包装响应 → 依赖注入会话/身份。
**所有**业务判定（发单窗、在途上限、状态机守卫、取件码可见性）都在
`OrderService` / `state_machine`，这里一个 `if` 都不写 ——
同一规则两处实现，早晚会出现"接口放行了、状态机拒绝"的不一致。

## 路由顺序是契约的一部分

`/hall` 必须声明在 `/{order_id}` **之前**：FastAPI 按声明顺序匹配，
反过来的话 `GET /orders/hall` 会先命中 `/{order_id}` 并把 `"hall"`
交给 `int` 解析，返回一个 `422` —— 而且报错信息是"输入不是整数"，
排查方向完全跑偏。

## 事务边界

service 层不 commit，提交由 `get_db_session` 依赖在请求正常返回后统一做。
唯一的例外是**没有例外**：抢单的 Redis 锁在 service 内释放（`finally`），
DB 的正确性由 `apply()` 的 CAS 保证，锁只是性能预过滤（详见
`OrderService.grab_order`）。
"""

from __future__ import annotations

from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, get_current_user, get_db_session
from app.core.response import Page, ok
from app.models.order import Order
from app.modules.order.service import OrderService
from app.modules.order.state_machine import ApplyResult
from app.schemas.order import CreateOrderRequest, ProgressRequest

router = APIRouter(prefix="/orders", tags=["orders"])


def _transition_payload(result: ApplyResult, **extra: Any) -> dict[str, Any]:
    """把 `ApplyResult` 压成前端需要的最小响应。

    刻意**不回** `status_patch` 全量（里面有 `settle_state` /
    `pre_dispute_status` 这类内部字段），只挑客户端要渲染的。
    `extra` 用于补 `accepted_at` 等本行字段。
    """
    data: dict[str, Any] = {
        "id": result.progress.order_id,
        "status": result.to_status,
        "action": result.action.value,
        "transition": result.transition_id,
        "seq": result.seq,
    }
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# 发单（T13）
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 大厅（FR-GRAB-01）—— 必须早于 /{order_id} 声明
# ---------------------------------------------------------------------------
@router.get("/hall", summary="大厅：待接单列表")
def hall(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """`PENDING` 且未过期的订单，按发布时间倒序，最多 50 条。

    响应**绝不含** `pick_code`（连密文都不查，见 `queries.list_hall` 的列白名单）。

    响应体用 K-05 的游标分页形状 `{items, next_cursor, has_more}`（本轮
    `next_cursor=null`、`has_more=false`：校园场景同时待接单在几十量级，
    `LIMIT 50` 一次拿完够用）。形状先定死，将来加分页时前端不用改解析逻辑 ——
    反过来先返回裸数组、以后再包一层，就是一次破坏性变更。
    """
    items = OrderService(db).hall(user_id=user.id, is_verified=user.is_verified)
    return JSONResponse(status_code=200, content=ok(Page(items).to_dict()))


# ---------------------------------------------------------------------------
# 详情（FR-CENTER-05）
# ---------------------------------------------------------------------------
@router.get("/{order_id}", summary="订单详情")
def get_order(
    order_id: int,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """订单详情 + `available_actions[]`。

    取件码只对当事人返回明文：发单人本人，或**已抢到该单**的接单人；
    其他人拿到 `pick_code=null, pick_code_masked=true`。
    """
    data = OrderService(db).detail(
        order_id=order_id, user_id=user.id, is_verified=user.is_verified
    )
    return JSONResponse(status_code=200, content=ok(data))


# ---------------------------------------------------------------------------
# 抢单（T20，最关键）
# ---------------------------------------------------------------------------
@router.post("/{order_id}/grab", summary="抢单（T20）")
def grab_order(
    order_id: int,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """抢单：Redis 锁预过滤 + 状态机 CAS 定胜负。

    五个可识别失败：`40901` 已被接走 / `40303` 自己的单 / `40902` 在途超限 /
    `40903` 非 PENDING / `42201` 守卫不满足（如已过期）。
    """
    result = OrderService(db).grab_order(
        order_id=order_id,
        runner_id=user.id,
        is_verified=user.is_verified,
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            _transition_payload(
                result,
                runner_id=result.status_patch.get("runner_id", user.id),
                accepted_at=result.status_patch.get("accepted_at"),
            ),
            "抢单成功",
        ),
    )


# ---------------------------------------------------------------------------
# 履约进度（T5 / T6 / T7）
# ---------------------------------------------------------------------------
@router.post("/{order_id}/progress", summary="上报履约进度（已取件/配送中/已送达）")
def update_progress(
    order_id: int,
    body: ProgressRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """节点只能顺序推进（`PICKED_UP → ON_THE_WAY → DELIVER`），跳级返回 `42201`。"""
    result = OrderService(db).advance_progress(
        order_id=order_id,
        user_id=user.id,
        action=body.action,
        is_verified=user.is_verified,
        note=body.note,
        images=tuple(body.images),
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            _transition_payload(
                result,
                is_late=result.status_patch.get("is_late"),
                auto_confirm_due_at=result.status_patch.get("auto_confirm_due_at"),
            ),
            "进度已更新",
        ),
    )


# ---------------------------------------------------------------------------
# 确认收货（T9）
# ---------------------------------------------------------------------------
@router.post("/{order_id}/confirm", summary="确认收货（T9）")
def confirm_order(
    order_id: int,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db_session)],
) -> JSONResponse:
    """确认收货（仅发单人；必须已 `DELIVERED`）。"""
    result = OrderService(db).confirm_receipt(
        order_id=order_id,
        user_id=user.id,
        is_verified=user.is_verified,
    )
    return JSONResponse(
        status_code=200,
        content=ok(
            _transition_payload(result, completed_at=result.status_patch.get("completed_at")),
            "确认收货成功",
        ),
    )


__all__ = ["router"]
