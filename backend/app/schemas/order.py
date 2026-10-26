"""订单请求/响应 DTO（T-13/T-14）。

Pydantic 只做**结构校验**（类型、长度、枚举），业务校验（酬劳区间、取件码必填、
发单窗、上限）放 service —— Pydantic 拦"格式错"，service 拦"业务不合规"，
两者职责分开，错误码也分开（400xx 参数 vs 42902 上限 / 40002 取件码）。

金额一律 `Decimal`，禁止 `float`（K-04 的反面教材：0.5 步进用 float 会丢精度）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 发单任务类型（FR-ORDER-01）。OTHER 由开关关闭（默认不开放）。
ORDER_TYPES: tuple[str, ...] = ("EXPRESS", "BUY", "SEND")

# BR-07：酬劳区间
REWARD_MIN: Decimal = Decimal("2")
REWARD_MAX: Decimal = Decimal("50")

# 描述上限（DB varchar(200)，但 UI 上 200 字已很长，这里收 200）
DESCRIPTION_MAX: int = 200

# FR-ORDER-10：取件码 4–20 位
PICK_CODE_MIN: int = 4
PICK_CODE_MAX: int = 20

# 图片上限（FR-ORDER-04：最多 3 张）
IMAGES_MAX: int = 3


class CreateOrderRequest(BaseModel):
    """发单入参（POST /orders）。"""

    type: Literal["EXPRESS", "BUY", "SEND"] = Field(description="任务类型")
    from_poi_id: int = Field(gt=0, description="起点 POI（BR-08 必须来自字典）")
    to_poi_id: int = Field(gt=0, description="终点 POI")
    reward: Decimal = Field(description="酬劳，2–50 元，0.5 步进")
    expect_minutes: Literal[30, 60, 120, 720] = Field(description="期望时长（分钟）")
    description: str | None = Field(default=None, max_length=DESCRIPTION_MAX)
    images: list[str] = Field(default_factory=list, max_length=IMAGES_MAX)
    pick_code: str | None = Field(default=None, description="取件码，type=EXPRESS 时必填")

    @field_validator("reward")
    @classmethod
    def _reward_range(cls, v: Decimal) -> Decimal:
        if v < REWARD_MIN or v > REWARD_MAX:
            raise ValueError(f"酬劳需在 {REWARD_MIN}–{REWARD_MAX} 元之间")
        # 0.5 步进校验（BR-07）
        if (v * 2) % 1 != 0:
            raise ValueError("酬劳需以 0.5 元为步进")
        return v

    @field_validator("pick_code")
    @classmethod
    def _pick_code_len(cls, v: str | None) -> str | None:
        if v is not None and not (PICK_CODE_MIN <= len(v) <= PICK_CODE_MAX):
            raise ValueError(f"取件码需 {PICK_CODE_MIN}–{PICK_CODE_MAX} 位")
        return v


class OrderBrief(BaseModel):
    """订单摘要（发单响应 / 列表）。**不含** `pick_code`（密文不外泄）。"""

    id: int
    order_no: str
    status: str
    deadline: str  # ISO 8601 带 Z
    type: str
    reward: str  # Decimal → str，避免 JS 浮点
    from_poi_id: int
    to_poi_id: int


class CreateOrderResponse(BaseModel):
    """发单成功响应。"""

    order: OrderBrief
