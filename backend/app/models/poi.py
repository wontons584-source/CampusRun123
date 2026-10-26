"""POI 字典（5.2 pois）。

BR-08：发单的起终点必须来自本表 —— 用户不能手填坐标。
坐标统一存 GCJ02（高德系），WGS84 在前端/导入脚本里转换。
**不建 PostGIS / GIST 地理索引**（ADR-02）：地理检索全走 Redis GEO，
本表的 lat/lng 只用于后台展示与 Redis 丢失后的冷备重算。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

POI_TYPES: tuple[str, ...] = (
    "STATION",
    "DORM",
    "CANTEEN",
    "BUILDING",
    "ADMIN",
    "PRINT",
    "SHOP",
)


class Poi(Base):
    """POI 字典表。"""

    __tablename__ = "pois"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    campus_id: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("1"),
        comment="预留多校区（M1 不做）",
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="STATION/DORM/CANTEEN/BUILDING/ADMIN/PRINT/SHOP",
    )
    lat: Mapped[Any] = mapped_column(
        Numeric(9, 6), nullable=False, comment="GCJ02（高德系）"
    )
    lng: Mapped[Any] = mapped_column(
        Numeric(9, 6), nullable=False, comment="GCJ02（高德系）"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
        comment="下架后新单不可选",
    )
    sort_hint: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="搜索排序权重，越大越靠前",
    )

    __table_args__ = (
        # 5.2：UNIQUE(campus_id, name)
        Index("uq_pois_campus_id_name", "campus_id", "name", unique=True),
        # 5.3：字典列表/筛选（FR-ORDER-02）
        Index("ix_pois_campus_id_is_active_type", "campus_id", "is_active", "type"),
        CheckConstraint(
            "type IN ('STATION', 'DORM', 'CANTEEN', 'BUILDING', 'ADMIN', 'PRINT', 'SHOP')",
            name="type_enum",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Poi id={self.id} name={self.name!r} type={self.type}>"
