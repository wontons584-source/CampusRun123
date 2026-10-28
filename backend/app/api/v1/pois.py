"""POI 字典 API（BR-08）。

`GET /api/v1/pois` —— 发单选点用的字典。**不加认证依赖**：
发单页的首屏就要三个下拉（起点/终点/类型），此时用户可能还没登录，
给字典接口加鉴权只会让前端多一次"登录后才能选地点"的死锁体验。
字典是公开信息（校园地图上的地点），不含任何用户数据。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db_session
from app.core.response import ok
from app.models.poi import Poi

router = APIRouter(prefix="/pois", tags=["pois"])


@router.get("", summary="POI 字典列表")
def list_pois(
    db: Annotated[Session, Depends(get_db_session)],
    type: Annotated[str | None, Query(description="按类型过滤（STATION/DORM/...）")] = None,
) -> JSONResponse:
    """返回上架中的 POI（`is_active=true`），按 `sort_hint DESC, id ASC` 排序。

    `sort_hint` 让"最常用的驿站/食堂"排在前面（种子数据里给了权重），
    比按 id 排序更符合用户找地点的直觉。
    """
    stmt = select(Poi).where(Poi.is_active.is_(True))
    if type:
        stmt = stmt.where(Poi.type == type.upper())
    stmt = stmt.order_by(Poi.sort_hint.desc(), Poi.id.asc())

    items: list[dict[str, Any]] = [
        {
            "id": int(poi.id),
            "name": poi.name,
            "type": poi.type,
            "lat": str(poi.lat),
            "lng": str(poi.lng),
        }
        for poi in db.execute(stmt).scalars().all()
    ]
    return JSONResponse(status_code=200, content=ok(items))


__all__ = ["router"]
