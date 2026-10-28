"""POI DTO（BR-08：发单起终点必须来自字典）。

列表只回 `{id,name,type,lat,lng}`：坐标是 GCJ02（高德系），前端直接拿去画图，
不需要再做坐标转换 —— 转换的责任在导入脚本，不在接口。
"""

from __future__ import annotations

from pydantic import BaseModel


class PoiOut(BaseModel):
    """一个 POI（GCJ02 坐标）。"""

    id: int
    name: str
    type: str
    lat: str
    lng: str


__all__ = ["PoiOut"]
