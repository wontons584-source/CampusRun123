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
import uuid
from dataclasses import replace
from decimal import Decimal
from typing import Any, Final

import redis as redis_lib
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import clock as default_clock
from app.core.errors import ApiError, ErrCode
from app.core.security import encrypt_pick_code
from app.db.redis import dependency_down, get_redis, grab_lock, grab_unlock
from app.models.poi import Poi
from app.modules.admin.configs import ConfigService
from app.modules.order.no_gen import gen_order_no
from app.modules.order.queries import (
    HALL_LIMIT,
    actor_for,
    detail_actions,
    hall_actions_for,
    list_hall,
    load_detail,
    reveal_pick_code,
)
from app.modules.order.repo import OrderDraft, SqlOrderRepo, count_inflight, count_pending
from app.modules.order.state_machine import (
    Action,
    Actor,
    ApplyResult,
    OrderCtx,
    Status,
    Thresholds,
    TransitionPayload,
    apply,
    guard_create,
)

logger = logging.getLogger("campusrun.order")

#: 抢单锁 TTL（毫秒）。3s 是「比一次 DB 事务长、比用户重试间隔短」的折中：
#: 太短则锁不住并发（正常一次 CAS 也要 10–30ms），太长则持锁进程崩掉后
#: 该单 3 秒内没人抢得动。TTL 只是兜底 —— 真正的互斥是 `cas_update_status`。
GRAB_LOCK_TTL_MS: Final[int] = 3000

#: 进度动作白名单（对外只暴露三个节点，见 `app.schemas.order.PROGRESS_ACTIONS`）。
PROGRESS_ACTIONS: Final[frozenset[Action]] = frozenset(
    {Action.PICKED_UP, Action.ON_THE_WAY, Action.DELIVER}
)


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

    # ------------------------------------------------------------------ 抢单
    def grab_order(
        self,
        *,
        order_id: int,
        runner_id: int,
        is_verified: bool = True,
        now: datetime.datetime | None = None,
    ) -> ApplyResult:
        """T2 抢单（BR-17）：Redis 锁预过滤 + 状态机 CAS 定胜负。

        ## 为什么"锁"和"CAS"都要有

        - **Redis 锁**（`SET NX PX`）：把 20 个并发请求挡在门外，只有 1 个能走到
          DB —— 省掉 19 次事务与 19 次 `SELECT ... FOR UPDATE`。它是**性能**手段。
        - **DB CAS**（`apply()` 里的 `WHERE status='PENDING'`）：把"比较"和"交换"
          压进数据库一行锁，是**正确性**手段。Redis 挂了/主从切换/锁提前过期都不会
          造成两个人抢到同一单 —— 只是退回"全靠 DB 串行"。

        两者顺序不可颠倒：先锁再 CAS，冲突代价最小；反过来就失去锁的意义。

        ## 四种失败必须给出四个不同的码（6.3）

        | 场景 | 码 | 前端文案方向 |
        |---|---|---|
        | 已被别人接走 | `409/40901` | 手慢了，去看看别单 |
        | 接自己的单 | `403/40303` | 不能接自己发的单 |
        | 在途超 3 单 | `409/40902` | 先把手上的单完成 |
        | 非 PENDING（已取消/已过期/已送达） | `409/40903` | 该单已不可接 |

        全部映射成 409 会让用户不知道该刷新还是该先完成手上的单。

        Args:
            order_id: 订单 id。
            runner_id: 抢单用户 id（来自 JWT）。
            is_verified: 是否已认证（`guard_grab` 的硬条件）。
            now: 注入时刻（测试用）。

        Returns:
            `ApplyResult`（`transition_id='T2'`，`status_patch` 含 `runner_id`）。

        Raises:
            ApiError: 404/40401、403/40301、403/40303、409/40901、409/40902、
                409/40903、422/42201、503/50300。
        """
        moment = default_clock.to_utc(now) if now is not None else default_clock.now()
        cfg = self._thresholds()

        # ---- 0. 先做零成本的前置判定，把 90% 的失败挡在 Redis 之外 ----
        #     （换成"先抢锁再校验"会让每次点"接自己的单"都白占一次 Redis 往返）
        if not is_verified:
            raise ApiError(403, ErrCode.NOT_VERIFIED, "请先完成认证后再接单")
        ctx = self._repo.load_ctx(order_id)          # 不存在 → 404/40401
        if ctx.publisher_id == runner_id:
            raise ApiError(403, ErrCode.CANNOT_GRAB_OWN_ORDER, "不能接自己发布的订单")
        if ctx.status != Status.PENDING.value:
            raise ApiError(
                409, ErrCode.ORDER_STATUS_NOT_GRABBABLE, "该订单当前不可接（已有人接走或已关闭）"
            )

        # ---- 0.5 在途数必须按**抢单人**统计，不能用 load_ctx 里的那个值 ----
        # `_LOAD_CTX_SQL` 的 `runner_inflight_count` 是「本单接单人的在途数」，
        # 而待接单的 `runner_id` 是 NULL —— 那个子查询对 PENDING 单恒等于 0。
        # 用它做 `guard_grab` 的 BR-16 判据，等于这条守卫永远放行（实测：手上
        # 已有 3 单的人还能继续抢第 4 单）。抢单场景的正确口径是"我要抢单，
        # 我自己手上有几单"，所以按 `runner_id` 现查一次，并用 `dataclasses.replace`
        # 覆盖进快照 —— 守卫读到的仍是同一份 `OrderCtx`，只是这一个字段被修正。
        inflight = count_inflight(self._db, runner_id)
        ctx = replace(ctx, runner_inflight_count=inflight)
        if inflight >= cfg.max_inflight:
            raise ApiError(
                409,
                ErrCode.INFLIGHT_LIMIT_REACHED,
                f"同时最多 {cfg.max_inflight} 单在途，请先完成手上的单",
            )

        # ---- 1. Redis 抢单锁（预过滤，不是正确性保证）----
        token = uuid.uuid4().hex
        try:
            client = get_redis()
            locked = grab_lock(client, order_id=order_id, token=token, ttl_ms=GRAB_LOCK_TTL_MS)
        except redis_lib.RedisError as exc:
            # Redis 不可用 → 503（而不是放行）。放行会让 20 个请求同时打 DB，
            # 虽然 CAS 仍能保证只有 1 个成功，但那是把"降级"做成"雪崩"。
            logger.warning("order.grab.redis_unavailable order_id=%s err=%s", order_id, exc)
            raise dependency_down(exc) from exc
        if not locked:
            raise ApiError(409, ErrCode.ORDER_ALREADY_TAKEN, "手慢了，订单已被接走")

        # ---- 2. 状态机（唯一改状态的出口）----
        try:
            return self._apply_grab(ctx, runner_id, moment, cfg)
        finally:
            # 无论成败都释放：CAS 已是权威，锁留着只会让重试白等 3 秒。
            # 释放前校验 token（`grab_unlock`），避免误删别人刚拿到的锁。
            try:
                grab_unlock(client, order_id=order_id, token=token)
            except redis_lib.RedisError:  # pragma: no cover - 释放失败只影响性能
                logger.warning("order.grab.unlock_failed order_id=%s", order_id)

    def _apply_grab(
        self,
        ctx: OrderCtx,
        runner_id: int,
        moment: datetime.datetime,
        cfg: Thresholds,
    ) -> ApplyResult:
        """抢占的 CAS 内核：把 `40905`（慢了一步）翻译成 `40901`（已被接走）。

        为什么要翻译：`40905` 的语义是"你手上的状态快照过期了，刷新重试"，
        那是给**同一个人**连点两次用的；抢单场景下用户真正需要知道的是
        "别人先抢到了"。两个码指向两种完全不同的动作（重试 vs 换一单）。
        """
        try:
            return apply(
                self._repo,
                ctx,
                Action.ACCEPT,
                Actor.runner(runner_id),
                now=moment,
                cfg=cfg,
            )
        except ApiError as exc:
            if exc.code == ErrCode.STATUS_CHANGED_CONCURRENTLY:
                raise ApiError(
                    409, ErrCode.ORDER_ALREADY_TAKEN, "手慢了，订单已被接走"
                ) from exc
            raise

    # -------------------------------------------------------------- 履约进度
    def advance_progress(
        self,
        *,
        order_id: int,
        user_id: int,
        action: Action | str,
        is_verified: bool = True,
        note: str | None = None,
        images: tuple[str, ...] = (),
        now: datetime.datetime | None = None,
    ) -> ApplyResult:
        """T5 / T6 / T7：接单人推进节点（已取件 / 配送中 / 已送达）。

        **不做任何顺序判断**：`PICKED_UP → ON_THE_WAY → DELIVER` 的约束由
        `_node_in_order` / `_last_node_reached` 守卫实现（跳级 → `42201`）。
        这里再判一次就有两份规则，改一处忘一处必然出错。

        Args:
            order_id: 订单 id。
            user_id: 当前登录用户（角色按与订单的关系推断，见 `actor_for`）。
            action: `PICKED_UP` / `ON_THE_WAY` / `DELIVER`（也可以带 `NOTE`）。
            is_verified: 认证标记（写进 `Actor`，供后续守卫使用）。
            note: 备注（`NOTE` 动作必填才有意义）。
            images: 送达凭证。
            now: 注入时刻（测试用；`DELIVER` 的 `is_late` 以它为基准）。

        Returns:
            `ApplyResult`。

        Raises:
            ApiError: 404/40401、403/40310、422/42201。
        """
        moment = default_clock.to_utc(now) if now is not None else default_clock.now()
        act = action if isinstance(action, Action) else Action(str(action))
        if act not in PROGRESS_ACTIONS and act is not Action.NOTE:
            # 白名单外的一律拒（例如 DELIVER 之外的 CONFIRM 不该从这个入口进来）
            raise ApiError(422, ErrCode.ILLEGAL_TRANSITION, f"不支持的进度动作：{act.value}")

        ctx = self._repo.load_ctx(order_id)
        actor = actor_for(ctx, user_id, is_verified=is_verified)
        return apply(
            self._repo,
            ctx,
            act,
            actor,
            TransitionPayload(note=note, images=tuple(images)),
            now=moment,
            cfg=self._thresholds(),
        )

    # -------------------------------------------------------------- 确认收货
    def confirm_receipt(
        self,
        *,
        order_id: int,
        user_id: int,
        is_verified: bool = True,
        now: datetime.datetime | None = None,
    ) -> ApplyResult:
        """T9 确认收货（DELIVERED → COMPLETED，仅发单人）。

        状态机 `guard_publisher_only` 会挡住接单人与陌生人；非 `DELIVERED`
        状态返回 `42201`（"还没送达，不能确认"）。

        Raises:
            ApiError: 404/40401、403/40310、422/42201。
        """
        moment = default_clock.to_utc(now) if now is not None else default_clock.now()
        ctx = self._repo.load_ctx(order_id)
        actor = actor_for(ctx, user_id, is_verified=is_verified)
        return apply(
            self._repo,
            ctx,
            Action.CONFIRM,
            actor,
            now=moment,
            cfg=self._thresholds(),
        )

    # ------------------------------------------------------------ 读侧（大厅/详情）
    def hall(
        self,
        *,
        user_id: int,
        is_verified: bool = True,
        now: datetime.datetime | None = None,
        limit: int = HALL_LIMIT,
    ) -> list[dict[str, Any]]:
        """大厅列表（`PENDING` 且未过期，倒序 `LIMIT 50`）。

        每条都带 `available_actions`：对已认证的接单人是 `["ACCEPT"]`，
        对他自己的单是 `["CANCEL"]`（发单人才能取消）。前端据此渲染按钮，
        不必自己复制一份"什么状态能接单"的规则。
        """
        actor = Actor.runner(user_id, is_verified=is_verified)
        actions = hall_actions_for(actor)
        items = list_hall(self._db, now=now, limit=limit)
        return [item.to_dict(actions=actions) for item in items]

    def detail(
        self,
        *,
        order_id: int,
        user_id: int,
        is_verified: bool = True,
    ) -> dict[str, Any]:
        """订单详情：`available_actions[]` + 按可见性规则解密取件码。"""
        ctx = self._repo.load_ctx(order_id)               # 不存在 → 404/40401
        detail = load_detail(self._db, order_id)
        actor = actor_for(ctx, user_id, is_verified=is_verified)
        return detail.to_dict(
            pick_code=reveal_pick_code(detail, user_id),
            actions=detail_actions(self._db, order_id, actor),
        )

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
