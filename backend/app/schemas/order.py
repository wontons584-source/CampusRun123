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


# ---------------------------------------------------------------------------
# 履约（T-20 抢单之后的节点推进，FR-FULFILL）
# ---------------------------------------------------------------------------
#: 接单人可推进的节点。**刻意不含 `DELIVER` 之外的终态动作**：
#: 确认收货是发单人的 `POST /orders/{id}/confirm`，不是进度动作。
PROGRESS_ACTIONS: tuple[str, ...] = ("PICKED_UP", "ON_THE_WAY", "DELIVER")


class ProgressRequest(BaseModel):
    """履约进度上报（`POST /orders/{id}/progress`）。

    `action` 只接受三个节点值，且顺序由状态机守卫强制
    （`PICKED_UP → ON_THE_WAY → DELIVER`，跳级一律 `422/42201`）。

    **不校验顺序**在这里：顺序是状态机的事（`_node_in_order`），
    如果 Pydantic 也判一次，就有两份规则、两个错误码，
    前端要处理两种"跳级失败"。DTO 只管"值是不是这三个之一"。
    """

    action: Literal["PICKED_UP", "ON_THE_WAY", "DELIVER"] = Field(description="推进到哪个节点")
    note: str | None = Field(default=None, max_length=DESCRIPTION_MAX, description="备注")
    images: list[str] = Field(
        default_factory=list, max_length=IMAGES_MAX, description="送达凭证（最多 3 张）"
    )


class OrderDetailOut(BaseModel):
    """订单详情（`GET /orders/{id}`）。

    `pick_code` 是**唯一**可能带明文取件码的字段：
    - 发单人本人 → 明文；
    - 已抢到该单的接单人（`status ∈ {IN_PROGRESS, DELIVERED, COMPLETED}`）→ 明文；
    - 其他人（含未抢单的接单人、陌生人）→ `None`，并置 `pick_code_masked=True`。

    这不是展示细节，是 FR-ORDER-10 的核心：取件码泄露 = 别人可以直接去驿站把件取走。
    """

    id: int
    order_no: str
    status: str
    type: str
    reward: str
    from_poi_id: int
    from_poi_name: str
    to_poi_id: int
    to_poi_name: str
    description: str | None = None
    images: list[str] = Field(default_factory=list)
    expect_minutes: int
    deadline: str
    created_at: str
    accepted_at: str | None = None
    delivered_at: str | None = None
    completed_at: str | None = None
    is_late: bool = False
    publisher_id: int
    runner_id: int | None = None
    publisher_nickname: str | None = None
    runner_nickname: str | None = None
    pick_code: str | None = Field(default=None, description="仅当事人可见明文")
    pick_code_masked: bool = Field(default=False, description="True 表示对该调用方不可见")
    available_actions: list[str] = Field(default_factory=list)


__all__ = [
    "DESCRIPTION_MAX",
    "IMAGES_MAX",
    "ORDER_TYPES",
    "PICK_CODE_MAX",
    "PICK_CODE_MIN",
    "PROGRESS_ACTIONS",
    "REWARD_MAX",
    "REWARD_MIN",
    "CreateOrderRequest",
    "CreateOrderResponse",
    "OrderBrief",
    "OrderDetailOut",
    "ProgressRequest",
]
