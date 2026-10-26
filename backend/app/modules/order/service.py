"""订单领域服务（T-13 发单；T-14 详情后续扩展）。

**事务边界**：发单走 `app.db.session.transaction()`，`create_order` 里
`orders` 行与 `CREATE` 事件同事务写入；CAS 冲突时整体回滚。service 层**不 commit**，
由 `transaction()` 上下文统一收口（K-09）。

**取件码的处理（FR-ORDER-10）**：
- `type=EXPRESS` 必填，缺 → `40002`（service 先拦，不让 DB 抛 500）；
- 非 EXPRESS 传了 → 忽略并告警（不是错误，是"用户填了多余字段"）；
- 明文只在入参里短暂存在，落库前用 `encrypt_pick_code` 转成 AES-GCM 密文；
  **明文禁止落日志、禁止进任何 DTO**。
"""

from __future__ import annotations

import datetime
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import clock as default_clock
from app.core.errors import ApiError, ErrCode
from app.core.security import encrypt_pick_code
from app.models.poi import Poi
from app.modules.admin.configs import ConfigService
from app.modules.order.no_gen import gen_order_no
from app.modules.order.repo import OrderDraft, SqlOrderRepo, count_pending
from app.modules.order.state_machine import (
    Actor,
    OrderCtx,
    Thresholds,
    guard_create,
)

logger = logging.getLogger("campusrun.order")


class OrderService:
    """发单 / 订单查询的领域入口。"""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._repo = SqlOrderRepo(db)
        self._cfg = ConfigService(db)

    # ------------------------------------------------------------------ 发单
    def create_order(
        self,
        *,
        publisher_id: int,
        is_verified: bool,
        type_: str,
        from_poi_id: int,
        to_poi_id: int,
        reward: Decimal,
        expect_minutes: int,
        description: str | None,
        images: tuple[str, ...],
        pick_code: str | None,
        now: datetime.datetime | None = None,
    ) -> int:
        """发单（T1）。返回新订单 id。

        校验顺序（每一条对应一个明确错误码，前端据此提示）：
        1. 取件码（FR-ORDER-10）
        2. POI 存在且 active（BR-08）
        3. 发单窗 + 待接单上限（`guard_create`，复用状态机守卫，不重写口径）
        4. 生成订单号 → 加密取件码 → `repo.create_order`
        """
        moment = default_clock.to_utc(now) if now is not None else default_clock.now()
        cfg = self._thresholds()

        # ---- 1. 取件码 ----
        pick_code_enc: bytes | None = None
        if type_ == "EXPRESS":
            if not pick_code:
                raise ApiError(400, ErrCode.PICK_CODE_REQUIRED, "代取快递必须提供取件码")
            pick_code_enc = encrypt_pick_code(pick_code)
        elif pick_code:
            # 非 EXPRESS 传了取件码：忽略但告警（不是错误，是冗余字段）
            logger.warning(
                "order.create: type=%s 传了 pick_code，已忽略（仅 EXPRESS 需要）", type_
            )

        # ---- 2. POI 存在且 active（BR-08）----
        if from_poi_id == to_poi_id:
            raise ApiError(400, ErrCode.INVALID_ARGUMENT, "起点和终点不能相同")
        pois = self._db.execute(
            select(Poi).where(Poi.id.in_([from_poi_id, to_poi_id]))
        ).scalars().all()
        poi_by_id = {p.id: p for p in pois}
        for pid, label in ((from_poi_id, "起点"), (to_poi_id, "终点")):
            poi = poi_by_id.get(pid)
            if poi is None:
                raise ApiError(404, ErrCode.POI_NOT_FOUND, f"{label}不存在")
            if not poi.is_active:
                raise ApiError(400, ErrCode.INVALID_ARGUMENT, f"{label}已下架，不可选择")

        # ---- 3. 发单窗 + 上限（guard_create 复用，不重写）----
        pending_count = count_pending(self._db, publisher_id)
        ctx = OrderCtx(
            id=0,  # 建单前无 id，只用于守卫
            status="PENDING",
            publisher_id=publisher_id,
            runner_id=None,
            accepted_at=None,
            delivered_at=None,
            has_runner_evidence=False,
            last_runner_evidence_at=None,
            pre_dispute_status=None,
            publisher_pending_count=pending_count,
        )
        actor = Actor.publisher(publisher_id, is_verified=is_verified)
        if not guard_create(ctx, actor, moment, cfg):
            # 区分「未认证」「不在发单窗」「待接单超限」给不同错误码
            if not is_verified:
                raise ApiError(403, ErrCode.NOT_VERIFIED, "请先完成学号认证")
            if pending_count >= cfg.max_pending_per_publisher:
                raise ApiError(429, ErrCode.PENDING_LIMIT_REACHED, "同时待接单不能超过 3 单")
            raise ApiError(400, ErrCode.INVALID_ARGUMENT, "当前不在可发单时间（07:00–23:00）")

        # ---- 4. deadline：今日内 → 当日 23:00（BR-09）----
        deadline = self._deadline_for(moment, expect_minutes)

        # ---- 5. 生成订单号 + 落库 ----
        from_poi = poi_by_id[from_poi_id]
        to_poi = poi_by_id[to_poi_id]
        draft = OrderDraft(
            order_no=gen_order_no(moment),
            publisher_id=publisher_id,
            type=type_,
            from_poi_id=from_poi_id,
            to_poi_id=to_poi_id,
            from_lat=Decimal(str(from_poi.lat)),
            from_lng=Decimal(str(from_poi.lng)),
            to_lat=Decimal(str(to_poi.lat)),
            to_lng=Decimal(str(to_poi.lng)),
            reward=reward,
            expect_minutes=expect_minutes,
            deadline=deadline,
            description=description,
            images=images,
            pick_code_enc=pick_code_enc,
        )
        return self._repo.create_order(draft, now=moment)

    # ------------------------------------------------------------------ 内部
    def _thresholds(self) -> Thresholds:
        """从 ConfigService 读阈值（T-06 热生效），缺省回落默认值。"""
        return Thresholds(
            silent_minutes=self._cfg.get_int("dispute.silent_minutes"),
            silent_warn_minutes=self._cfg.get_int("dispute.silent_warn_minutes"),
            auto_confirm_hours=self._cfg.get_int("order.auto_confirm_hours"),
            dispute_window_hours=self._cfg.get_int("dispute.window_hours"),
            review_window_days=self._cfg.get_int("review.window_days"),
            cancel_request_ttl_minutes=self._cfg.get_int("cancel.request_ttl_minutes"),
            max_inflight=self._cfg.get_int("grab.max_inflight"),
            max_pending_per_publisher=self._cfg.get_int("grab.max_pending_per_publisher"),
            max_urge_per_order=self._cfg.get_int("urge.max_per_order"),
            send_window_start_hour=self._cfg.get_int("order.send_window_start_hour"),
            send_window_end_hour=self._cfg.get_int("order.send_window_end_hour"),
        )

    def _deadline_for(self, moment: datetime.datetime, expect_minutes: int) -> datetime.datetime:
        """按期望时长算 deadline（BR-09）。

        `expect_minutes=720`（今日内）→ 当日 23:00（校园时区）；
        其余（30/60/120）→ `now + expect_minutes`。
        """
        if expect_minutes == 720:
            local = default_clock.to_local(moment)
            end_of_day = local.replace(hour=23, minute=0, second=0, microsecond=0)
            return end_of_day.astimezone(datetime.UTC)
        return moment + datetime.timedelta(minutes=expect_minutes)


__all__ = ["OrderService"]
