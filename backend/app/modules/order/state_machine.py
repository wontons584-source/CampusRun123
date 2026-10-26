"""订单状态机内核（T-12，验收项 D3）。

## 编号权威

`TRANSITIONS` 里的 `T1`–`T17` **逐条照搬 `docs/01-PRD.md` 第 363–383 行的迁移清单**
（`02-ARCHITECTURE` 7.4 与 5.4 是同一张表的转述）。编号是验收依据，**不许改、不许合并**：
- `T1`–`T17` 是**状态迁移号**（无连字符、无前导零）；
- `T-12` 是**开发任务号**（有连字符）。两者不可混写（13.0）。

## 为什么守卫是纯函数

16 章的老问题：状态机要能单测，而守卫一旦直接查库，单测就必须有 PG。
所以守卫签名固定为 `guard(ctx, actor, now, cfg=DEFAULT_THRESHOLDS, action=None) -> bool`，
**不接收 db、不接收 ORM 对象**；真实链路（T-16/T-20）先用 repo 查出 `OrderCtx`
再调 `apply()`。这样「T16/T17 互斥」「45 分钟边界」「1000 次投影」全部零依赖可测。

## 三个容易写错的口径（都在这里定死，别处只引用）

1. **`RUNNER_EVIDENCE`**（7.4 唯一定义）：`PICKED_UP / ON_THE_WAY / DELIVER / NOTE`
   **且 `actor_id == runner_id`**。发单人的 `NOTE`（催单）、`URGE`、`REQUEST_CANCEL`、
   `REJECT_CANCEL` **都不算进度** —— 否则发单人第 10 分钟催一次单，
   就把自己唯一的零人工出口 T17 永久关死，而 T16 又要求静默 ≥45min，
   两条路同时不可走，比原 bug 更糟。`probe_progress()` 是这条口径的唯一实现。
2. **T16/T17 的互斥在守卫层，不在运行时**：两个守卫读同一个 `probe_progress`，
   一个要求 `has_progress=True`、一个要求 `False`。同一时刻必有一个返回 False，
   所以并发、重放、连点都无法同时穿过 —— 不需要「用过就关」的标记字段。
3. **`pre_dispute_status` 只有一个写者**：进 DISPUTED 时写（T11/T12/T16），
   出 DISPUTED 时置 NULL（T13/T14/T15）。中途不许改（RSK-9）。

## 事务与 CAS

`apply()` 是**唯一**允许改 `orders.status` 的出口（5.4）：
校验迁移 → 跑守卫 → **先 append `order_progress` 事实** → 再 CAS `UPDATE orders`。
CAS 的 `WHERE status=<迁移前状态>` 是并发互斥的全部依据：0 行即说明别人先动了，
抛 `40905` 让客户端刷新重来（**不是** 42201，那不是"非法迁移"，是"你慢了一步"）。
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from app.core.clock import clock as default_clock
from app.core.errors import ApiError, ErrCode

# ---------------------------------------------------------------------------
# 枚举（K-03：值与 DB 里的 varchar 逐字一致，禁止字符串字面量散落）
# ---------------------------------------------------------------------------


class Status(StrEnum):
    """订单 7 状态（5.4）。`DISPUTED` 是**挂起态、非终态**（BR-34）。"""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DELIVERED = "DELIVERED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    DISPUTED = "DISPUTED"


#: 终态：没有任何出边。`DISPUTED` **不在**其中（BR-34）。
TERMINAL_STATUSES: Final[frozenset[Status]] = frozenset({Status.CANCELLED, Status.EXPIRED})


class Action(StrEnum):
    """`order_progress.action` 全集 **19 个**（5.4 的 18 个 + 补入的 `URGE`）。

    `URGE`（催单）为什么必须在全集里而不是放 Redis：BR-35 的"骚扰"判定与仲裁举证
    都要回答"发单人到底催了几次"（FR-FULFILL-05 每单最多 2 次）。Redis 重启即清零，
    而 `order_progress` 是 append-only 事实流，天然可审计。
    `REVIEW`（评价）**不入全集**：唯一载体是 `reviews` 表（`UNIQUE(order_id, from_id)`），
    再写一条 progress 是重复事实，只会给投影对账添噪声。
    """

    CREATE = "CREATE"
    ACCEPT = "ACCEPT"
    PICKED_UP = "PICKED_UP"
    ON_THE_WAY = "ON_THE_WAY"
    NOTE = "NOTE"
    URGE = "URGE"
    DELIVER = "DELIVER"
    CONFIRM = "CONFIRM"
    AUTO_CONFIRM = "AUTO_CONFIRM"
    REQUEST_CANCEL = "REQUEST_CANCEL"
    APPROVE_CANCEL = "APPROVE_CANCEL"
    REJECT_CANCEL = "REJECT_CANCEL"
    CANCEL = "CANCEL"
    CANCEL_LOST = "CANCEL_LOST"
    EXPIRE = "EXPIRE"
    DISPUTE_OPENED = "DISPUTE_OPENED"
    ARBITRATE_FREE = "ARBITRATE_FREE"
    ARBITRATE_LIABLE = "ARBITRATE_LIABLE"
    FORCE_CLOSE = "FORCE_CLOSE"


ACTIONS: Final[tuple[Action, ...]] = tuple(Action)
PROGRESS_ACTIONS: Final[frozenset[Action]] = frozenset(Action)


class Role(StrEnum):
    """`order_progress.actor_role` 的 4 个取值。"""

    PUBLISHER = "PUBLISHER"
    RUNNER = "RUNNER"
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"


# ---------------------------------------------------------------------------
# 常量口径
# ---------------------------------------------------------------------------

#: 7.4 唯一定义：什么算「接单人已产生进度」。
#: 条件 = `action ∈ 本白名单` **AND** `actor_id == order.runner_id`。
RUNNER_EVIDENCE: Final[tuple[str, ...]] = ("PICKED_UP", "ON_THE_WAY", "DELIVER", "NOTE")

#: 节点顺序（T5「节点只能顺序推进」、T7「已到最后节点」）。
NODE_ORDER: Final[dict[str, int]] = {"PICKED_UP": 1, "ON_THE_WAY": 2, "DELIVER": 3}

#: 最后节点：`DELIVER` 前必须先到 `ON_THE_WAY`（跳级 PICKED_UP→DELIVER 必须被拒）。
_LAST_NODE_BEFORE_DELIVER: Final[str] = "ON_THE_WAY"

#: `Trans.to_status` 的动态哨兵：目标态 = `order.pre_dispute_status`（T13/T14）。
PRE_DISPUTE: Final[str] = "<PRE_DISPUTE>"


class TransKind(StrEnum):
    """迁移种类。只有 `T` 会进 `TRANSITIONS` 的 17 条（其余是辅助动作）。"""

    T = "T"                                  # T1–T17，PRD 权威编号
    SELF_LOOP = "SELF_LOOP"                  # 5.4 列出的自环动作，不占 T 编号
    FREEZE_EVIDENCE = "FREEZE_EVIDENCE"      # 补丁 B：冻结期只补证据、不改 status
    ADMIN_OVERRIDE = "ADMIN_OVERRIDE"         # BR-32 强制关闭兜底


@dataclass(frozen=True)
class Thresholds:
    """业务阈值。

    落点说明：**最终唯一来源是 `sys_configs`**（K-11 / T-06 的 `ConfigService`）。
    T-06 尚未交付，本类的默认值与 7.4 的 `DEFAULTS` 逐项一致；
    T-06 接入后由 `Thresholds.from_config(cfg)` 注入，本文件的默认值只供单测。
    """

    silent_minutes: int = 45              # `dispute.silent_minutes`（BR-28/BR-33）
    silent_warn_minutes: int = 40         # `dispute.silent_warn_minutes`（BR-35）
    auto_confirm_hours: int = 24          # `order.auto_confirm_hours`（BR-21）
    dispute_window_hours: int = 48        # `dispute.window_hours`（BR-28 T12）
    review_window_days: int = 7           # `review.window_days`（BR-24）
    cancel_request_ttl_minutes: int = 30  # 协商取消申请有效期（T8）
    max_inflight: int = 3                 # `grab.max_inflight`（BR-16）
    max_pending_per_publisher: int = 3    # BR-11：同时待接单 ≤3
    max_urge_per_order: int = 2           # FR-FULFILL-05：每单最多催 2 次
    send_window_start_hour: int = 7       # BR-10 发单窗（校园时区）
    send_window_end_hour: int = 23        # BR-10 发单窗（不含 23 点）

    @property
    def silent_delta(self) -> datetime.timedelta:
        return datetime.timedelta(minutes=self.silent_minutes)

    @property
    def silent_warn_delta(self) -> datetime.timedelta:
        return datetime.timedelta(minutes=self.silent_warn_minutes)

    @property
    def auto_confirm_delta(self) -> datetime.timedelta:
        return datetime.timedelta(hours=self.auto_confirm_hours)

    @property
    def dispute_window_delta(self) -> datetime.timedelta:
        return datetime.timedelta(hours=self.dispute_window_hours)

    @property
    def review_window_delta(self) -> datetime.timedelta:
        return datetime.timedelta(days=self.review_window_days)


DEFAULT_THRESHOLDS: Final[Thresholds] = Thresholds()


# ---------------------------------------------------------------------------
# 参与者与上下文
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Actor:
    """操作者。`id=None` 只允许 `SYSTEM`（系统动作没有 user id）。"""

    id: int | None
    role: Role
    is_verified: bool = True

    @classmethod
    def system(cls) -> Actor:
        return cls(id=None, role=Role.SYSTEM, is_verified=True)

    @classmethod
    def admin(cls, admin_id: int) -> Actor:
        return cls(id=admin_id, role=Role.ADMIN, is_verified=True)

    @classmethod
    def publisher(cls, user_id: int, *, is_verified: bool = True) -> Actor:
        return cls(id=user_id, role=Role.PUBLISHER, is_verified=is_verified)

    @classmethod
    def runner(cls, user_id: int, *, is_verified: bool = True) -> Actor:
        return cls(id=user_id, role=Role.RUNNER, is_verified=is_verified)


@dataclass(frozen=True)
class OrderCtx:
    """守卫需要知道的**全部**事实快照 —— 一份不可变值对象，从 repo 查出来后传入。

    前 9 个字段是任务书明确要求的最小集；其余字段带默认值，因为守卫必须能判
    「节点顺序（T5）」「是否过期（T2/T4）」「在途/待接单上限（T2/T1）」
    「投诉窗口（T12）」「24h 到期（T10）」「协商申请是否已同意（T8）」——
    没有这些字段这些守卫只能返回常量，也就等于没有守卫。
    **新增字段一律带默认值**，这样老调用点不会因为加字段而炸。
    """

    # --- 任务书要求的 9 个必填字段 ---
    id: int
    status: str
    publisher_id: int
    runner_id: int | None
    accepted_at: datetime.datetime | None
    delivered_at: datetime.datetime | None
    has_runner_evidence: bool
    last_runner_evidence_at: datetime.datetime | None
    pre_dispute_status: str | None

    # --- 守卫需要的补充事实（有默认值，见类文档） ---
    deadline: datetime.datetime | None = None
    completed_at: datetime.datetime | None = None
    auto_confirm_due_at: datetime.datetime | None = None
    last_node: str | None = None            # 最近一次到达的节点（PICKED_UP/ON_THE_WAY）
    dispute_count: int = 0                  # BR-28 同单仅 1 次纠纷
    cancel_request_status: str | None = None  # 最近一条协商申请的 status
    runner_inflight_count: int = 0          # BR-16 接单人当前在途数
    publisher_pending_count: int = 0        # BR-11 发单人当前待接单数
    urge_count: int = 0                     # FR-FULFILL-05 本单已催次数（上限 2）
    order_type: str = "EXPRESS"

    # ---------------- 派生量 ----------------
    @property
    def silent_since(self) -> datetime.datetime | None:
        """静默起点（7.4）：有进度 → 最后一条 `RUNNER_EVIDENCE` 时刻；无 → 抢单时刻。"""
        if self.has_runner_evidence:
            return self.last_runner_evidence_at or self.accepted_at
        return self.accepted_at

    @property
    def has_progress(self) -> bool:
        """语义别名：T17 的守卫读它，语义比 `has_runner_evidence` 更贴近 PRD 措辞。"""
        return self.has_runner_evidence

    def is_party(self, actor: Actor) -> bool:
        if actor.id is None:
            return False
        return actor.id == self.publisher_id or (
            self.runner_id is not None and actor.id == self.runner_id
        )


# ---------------------------------------------------------------------------
# 守卫（纯函数）
# ---------------------------------------------------------------------------
def _party_guard(ctx: OrderCtx, actor: Actor) -> bool:
    if actor.role is Role.PUBLISHER:
        return actor.id == ctx.publisher_id
    if actor.role is Role.RUNNER:
        return ctx.runner_id is not None and actor.id == ctx.runner_id
    return False


def guard_create(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T1：已认证；发单时间窗内（BR-10 07:00–23:00）；同时待接单 ≤3（BR-11）。"""
    if actor.role is not Role.PUBLISHER or not actor.is_verified:
        return False
    local_hour = default_clock.to_local(now).hour
    if not cfg.send_window_start_hour <= local_hour < cfg.send_window_end_hour:
        return False
    return ctx.publisher_pending_count < cfg.max_pending_per_publisher


def guard_grab(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T2：原子抢占（CAS 承担）；在途 ≤3（BR-16）；未过期；不能接自己的单。

    「原子抢占」本身不在这里判 —— 它是 `apply()` 的 CAS `WHERE status='PENDING'`。
    Redis Lua 只是召回期预过滤（ADR-03），DB 才是权威。
    """
    if actor.role is not Role.RUNNER or not actor.is_verified:
        return False
    if ctx.runner_id is not None:
        return False
    if actor.id is not None and actor.id == ctx.publisher_id:
        return False
    if ctx.deadline is not None and now >= ctx.deadline:
        return False
    return ctx.runner_inflight_count < cfg.max_inflight


def guard_publisher_only(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T3 / T9：仅发单人本人。"""
    return actor.role is Role.PUBLISHER and actor.id == ctx.publisher_id


def guard_deadline_passed(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T4：系统动作，且确实已过期（防止误关未到期的单）。"""
    if actor.role is not Role.SYSTEM:
        return False
    return ctx.deadline is not None and now >= ctx.deadline


def _node_in_order(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T5：接单人本人 + 节点只能**顺序**推进（严格读法：只能推进到下一个节点）。

    ## 为什么是 `target == current + 1` 而不是「单调递增」(target > current)

    单调递增允许跳级：`last_node=None` 时可以直接点 `ON_THE_WAY`（跳过「已取件」），
    再从 `ON_THE_WAY` 点 `DELIVER` 完成整单 —— 时间轴上**永远没有 `PICKED_UP`**。
    后果不是"少一条日志"，而是：

    - **证据链断了**：「已取件」是接单人自证"东西已在我手上"的唯一节点，
      也是 T16（失联投诉）/ T17（无责取消）判 `has_runner_evidence` 的基础，
      更是仲裁时"他到底去没去驿站"的唯一凭据（RSK-11 已把这块标为固有盲区，
      绝不能我们自己再扩大盲区）。
    - **放宽没有任何产品收益**：忘了点的接单人只需补点一次（先点已取件、再点配送中），
      而"规则奖励报一声"正是本设计想要的引导。

    因此裁定为严格顺序推进，跳级一律 42201。
    """
    if actor.role is not Role.RUNNER or ctx.runner_id is None or actor.id != ctx.runner_id:
        return False
    if action is None:
        return False
    target = NODE_ORDER.get(str(action))
    if target is None:
        return False
    if ctx.last_node is None:
        current = 0                      # 尚未到任何节点
    else:
        current = NODE_ORDER.get(ctx.last_node)
        if current is None:
            return False                 # 脏数据（未知节点）不猜，直接拒
    return target == current + 1


def guard_runner_only(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T6：接单人本人留备注（不改节点，但重置静默计时并永久关闭 T17 通道）。"""
    return actor.role is Role.RUNNER and ctx.runner_id is not None and actor.id == ctx.runner_id


def _last_node_reached(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T7：接单人本人 + **已到最后一个节点**（必须是 `ON_THE_WAY`，跳级拒绝）。

    与 T5 保持一致用 `==` 而不是 `>=`：T5 收紧成严格顺序推进后，`last_node` 只可能
    是 `None / PICKED_UP / ON_THE_WAY` 三者之一（`DELIVER` 一旦写入，状态已经离开
    `IN_PROGRESS`）。写成 `>=` 在这里等价，但 `==` 更能表达"已到**最后**一个节点"
    这个语义，也让将来若新增节点时不会静默放宽。
    """
    if actor.role is not Role.RUNNER or ctx.runner_id is None or actor.id != ctx.runner_id:
        return False
    return NODE_ORDER.get(ctx.last_node or "") == NODE_ORDER[_LAST_NODE_BEFORE_DELIVER]


def guard_peer_agreed_cancel(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T8：协商取消，双方之一操作且**对方已同意**（`order_cancel_requests.status='APPROVED'`）。"""
    if not _party_guard(ctx, actor):
        return False
    return ctx.cancel_request_status == "APPROVED"


def guard_auto_confirm_due(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T10：系统动作 + `auto_confirm_due_at <= now`。

    **只读 `auto_confirm_due_at`，不读 `delivered_at`**（BR-21/BR-34）：
    T14 回滚后 `delivered_at` 是 3 天前，用它会在同一毫秒满足 24h。
    """
    if actor.role is not Role.SYSTEM:
        return False
    due = ctx.auto_confirm_due_at
    return due is not None and now >= due


def guard_party_single_dispute(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T11：任一方 + 同单仅 1 次纠纷（BR-28）。"""
    return _party_guard(ctx, actor) and ctx.dispute_count == 0


def guard_party_within_dispute_window(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T12：任一方 + 完成后 ≤48h（BR-28）+ 同单仅 1 次纠纷。

    「同单仅一次」虽不是 T12 行原文，但 BR-28 与 `disputes UNIQUE(order_id)`
    都要求它；守卫先拦能返回 `42201`（可读），否则会撞唯一约束变成 500。
    """
    if not _party_guard(ctx, actor) or ctx.dispute_count != 0:
        return False
    if ctx.completed_at is None:
        return False
    return now - ctx.completed_at <= cfg.dispute_window_delta


def guard_admin_free_in_progress(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T13：管理员 + `pre_dispute_status = IN_PROGRESS`（BR-34 回滚到进行中）。"""
    if actor.role is not Role.ADMIN:
        return False
    return ctx.pre_dispute_status == Status.IN_PROGRESS


def guard_admin_free_settled(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T14：管理员 + `pre_dispute_status ∈ {DELIVERED, COMPLETED}`（BR-34 回原状态）。"""
    if actor.role is not Role.ADMIN:
        return False
    return ctx.pre_dispute_status in (Status.DELIVERED, Status.COMPLETED)


def guard_admin_liable(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T15：管理员裁决担责（无额外前置条件）。"""
    return actor.role is Role.ADMIN


def guard_silent_45m(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T16 失联投诉守卫：**仅发单人** + **已产生过进度** + 静默 ≥45min（免举证）。

    阈值比较用 `>=`：44:59 拒、45:00 过（T-12 的边界判据）。

    额外带一条 `dispute_count == 0`：T16 行原文没写，但 BR-28「同单仅 1 次纠纷」
    与 `disputes UNIQUE(order_id)` 都要求它。守卫先拦能返回 `42201`（可读），
    否则会撞唯一约束变成 500 —— 而且 T13 回滚后订单会重新回到 IN_PROGRESS，
    此时再点投诉必须被挡住。
    """
    if actor.role is not Role.PUBLISHER or actor.id != ctx.publisher_id:
        return False
    if not ctx.has_runner_evidence or ctx.dispute_count != 0:
        return False
    since = ctx.silent_since
    if since is None:
        return False
    return now - since >= cfg.silent_delta


def guard_no_progress_45m(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """T17 失联自助取消守卫：**仅发单人** + **无任何进度** + 距抢单 ≥45min（BR-33）。

    与 T16 靠 `has_runner_evidence` 硬互斥（不是"用过就关"，后者挡不住并发与重放）。
    """
    if actor.role is not Role.PUBLISHER or actor.id != ctx.publisher_id:
        return False
    if ctx.has_runner_evidence:
        return False
    if ctx.accepted_at is None:
        return False
    return now - ctx.accepted_at >= cfg.silent_delta


def guard_admin_force_close(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """BR-32 / T-30：管理员强制关闭（唯一线上事故兜底）。`ctx` 不参与判断。"""
    return actor.role is Role.ADMIN


#: 守卫签名。`cfg` 与 `action` 带默认值，因此 `guard(ctx, actor, now)` 永远可调用。
GuardFn = Callable[..., bool]


# ---------------------------------------------------------------------------
# 迁移表
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Trans:
    """一条迁移定义。

    `effects` 只放**事务提交后要派发**的副作用（通知/推送/信誉），
    而 `orders` 行上的字段写入（`accepted_at` / `delivered_at` /
    `auto_confirm_due_at` / `pre_dispute_status` / `completed_at`）统一由
    `status_patch()` 产出 —— 一次事务里同源同刻，禁止在 service 层各写各的（7.4）。
    """

    id: str                                        # "T1".."T17" / "SL1" / "PF1" / "AO1"
    kind: TransKind
    from_status: Status | None                     # None = 不存在前置状态（T1）
    actions: tuple[Action, ...]
    to_status: Status | str                        # Status 或 PRE_DISPUTE 哨兵
    actors: frozenset[Role]
    guard: GuardFn
    effects: tuple[str, ...] = ()
    hint: str = ""                                 # 守卫失败给用户看的话
    summary: str = ""                              # 达人读的迁移描述

    @property
    def mutates_status(self) -> bool:
        """是否会改 `orders.status`（投影对账用，见 `STATUS_MUTATING`）。"""
        if self.kind is TransKind.FREEZE_EVIDENCE:
            return False
        if self.to_status == PRE_DISPUTE:
            return True
        if self.from_status is None:
            return True
        return self.to_status != self.from_status


_TRANSITIONS_LIST: tuple[Trans, ...] = (
    Trans(
        id="T1",
        kind=TransKind.T,
        from_status=None,
        actions=(Action.CREATE,),
        to_status=Status.PENDING,
        actors=frozenset({Role.PUBLISHER}),
        guard=guard_create,
        effects=("notify_noop", "enqueue_first_push"),
        hint="当前不在发单时间窗内，或同时待接单已达 3 单",
        summary="— → PENDING：发布订单",
    ),
    Trans(
        id="T2",
        kind=TransKind.T,
        from_status=Status.PENDING,
        actions=(Action.ACCEPT,),
        to_status=Status.IN_PROGRESS,
        actors=frozenset({Role.RUNNER}),
        guard=guard_grab,
        effects=("notify_publisher", "cancel_push"),
        hint="该单当前不可接（已过期 / 已有人在途 / 是你自己的单）",
        summary="PENDING → IN_PROGRESS：抢单",
    ),
    Trans(
        id="T3",
        kind=TransKind.T,
        from_status=Status.PENDING,
        actions=(Action.CANCEL,),
        to_status=Status.CANCELLED,
        actors=frozenset({Role.PUBLISHER}),
        guard=guard_publisher_only,
        effects=("cancel_push",),
        hint="只有发单人能取消未接单的订单",
        summary="PENDING → CANCELLED：取消",
    ),
    Trans(
        id="T4",
        kind=TransKind.T,
        from_status=Status.PENDING,
        actions=(Action.EXPIRE,),
        to_status=Status.EXPIRED,
        actors=frozenset({Role.SYSTEM}),
        guard=guard_deadline_passed,
        effects=("cancel_push",),
        hint="订单尚未到期",
        summary="PENDING → EXPIRED：到期未接",
    ),
    # T5 一条 Trans 覆盖两个 action（PICKED_UP / ON_THE_WAY 都是「推进节点」）。
    # 拆成两条会让 TRANSITIONS 变成 18 条，与 PRD 的 17 条对不上。
    Trans(
        id="T5",
        kind=TransKind.T,
        from_status=Status.IN_PROGRESS,
        actions=(Action.PICKED_UP, Action.ON_THE_WAY),
        to_status=Status.IN_PROGRESS,
        actors=frozenset({Role.RUNNER}),
        guard=_node_in_order,
        effects=("notify_publisher",),
        hint="节点只能顺序推进，或你不是这单的接单人",
        summary="IN_PROGRESS → IN_PROGRESS：推进节点（已取件/配送中）",
    ),
    Trans(
        id="T6",
        kind=TransKind.T,
        from_status=Status.IN_PROGRESS,
        actions=(Action.NOTE,),
        to_status=Status.IN_PROGRESS,
        actors=frozenset({Role.RUNNER}),
        guard=guard_runner_only,
        effects=("notify_publisher",),
        hint="只有接单人本人能留进度备注",
        summary="IN_PROGRESS → IN_PROGRESS：留备注（重置静默计时，永久关闭 T17）",
    ),
    Trans(
        id="T7",
        kind=TransKind.T,
        from_status=Status.IN_PROGRESS,
        actions=(Action.DELIVER,),
        to_status=Status.DELIVERED,
        actors=frozenset({Role.RUNNER}),
        guard=_last_node_reached,
        effects=("notify_publisher", "mark_late"),
        hint="还没到最后节点（请先点「已取件」「配送中」）",
        summary="IN_PROGRESS → DELIVERED：标记已送达",
    ),
    Trans(
        id="T8",
        kind=TransKind.T,
        from_status=Status.IN_PROGRESS,
        actions=(Action.APPROVE_CANCEL,),
        to_status=Status.CANCELLED,
        actors=frozenset({Role.PUBLISHER, Role.RUNNER}),
        guard=guard_peer_agreed_cancel,
        effects=("release_inflight", "notify_both"),
        hint="协商取消需要对方先同意",
        summary="IN_PROGRESS → CANCELLED：协商取消",
    ),
    Trans(
        id="T9",
        kind=TransKind.T,
        from_status=Status.DELIVERED,
        actions=(Action.CONFIRM,),
        to_status=Status.COMPLETED,
        actors=frozenset({Role.PUBLISHER}),
        guard=guard_publisher_only,
        effects=("credit_add", "open_review", "notify_runner"),
        hint="只有发单人能确认收货",
        summary="DELIVERED → COMPLETED：确认收货",
    ),
    Trans(
        id="T10",
        kind=TransKind.T,
        from_status=Status.DELIVERED,
        actions=(Action.AUTO_CONFIRM,),
        to_status=Status.COMPLETED,
        actors=frozenset({Role.SYSTEM}),
        guard=guard_auto_confirm_due,
        effects=("credit_add", "open_review", "flag_auto"),
        hint="未到自动确认时间（auto_confirm_due_at）",
        summary="DELIVERED → COMPLETED：24h 自动确认",
    ),
    Trans(
        id="T11",
        kind=TransKind.T,
        from_status=Status.DELIVERED,
        actions=(Action.DISPUTE_OPENED,),
        to_status=Status.DISPUTED,
        actors=frozenset({Role.PUBLISHER, Role.RUNNER}),
        guard=guard_party_single_dispute,
        effects=("freeze", "hide_reviews", "notify_both"),
        hint="该单已有纠纷记录（同单仅 1 次）",
        summary="DELIVERED → DISPUTED：发起投诉",
    ),
    Trans(
        id="T12",
        kind=TransKind.T,
        from_status=Status.COMPLETED,
        actions=(Action.DISPUTE_OPENED,),
        to_status=Status.DISPUTED,
        actors=frozenset({Role.PUBLISHER, Role.RUNNER}),
        guard=guard_party_within_dispute_window,
        effects=("freeze", "hide_reviews", "notify_both"),
        hint="已超过完成后的 48 小时投诉窗口",
        summary="COMPLETED → DISPUTED：完成后 48h 内投诉",
    ),
    # T13 / T14 共用 (DISPUTED, ARBITRATE_FREE) 这个键 —— 字典放不下两个，
    # 所以索引是「键 → 候选列表」，按守卫顺序取第一个通过的（两者互斥，结果确定）。
    Trans(
        id="T13",
        kind=TransKind.T,
        from_status=Status.DISPUTED,
        actions=(Action.ARBITRATE_FREE,),
        to_status=Status.IN_PROGRESS,
        actors=frozenset({Role.ADMIN}),
        guard=guard_admin_free_in_progress,
        effects=("unfreeze", "reopen_review", "notify_both"),
        hint="投诉前状态不是「进行中」，应走 T14",
        summary="DISPUTED → IN_PROGRESS：裁决无责（PRE=IN_PROGRESS）",
    ),
    Trans(
        id="T14",
        kind=TransKind.T,
        from_status=Status.DISPUTED,
        actions=(Action.ARBITRATE_FREE,),
        to_status=PRE_DISPUTE,
        actors=frozenset({Role.ADMIN}),
        guard=guard_admin_free_settled,
        effects=("unfreeze", "reopen_review", "notify_both"),
        hint="投诉前状态不在 {DELIVERED, COMPLETED}，应走 T13",
        summary="DISPUTED → pre_dispute_status：裁决无责（PRE=DELIVERED/COMPLETED）",
    ),
    Trans(
        id="T15",
        kind=TransKind.T,
        from_status=Status.DISPUTED,
        actions=(Action.ARBITRATE_LIABLE,),
        to_status=Status.CANCELLED,
        actors=frozenset({Role.ADMIN}),
        guard=guard_admin_liable,
        effects=("credit_penalty", "unfreeze", "notify_both"),
        hint="仅管理员可裁决",
        summary="DISPUTED → CANCELLED：裁决担责",
    ),
    Trans(
        id="T16",
        kind=TransKind.T,
        from_status=Status.IN_PROGRESS,
        actions=(Action.DISPUTE_OPENED,),
        to_status=Status.DISPUTED,
        actors=frozenset({Role.PUBLISHER}),
        guard=guard_silent_45m,
        effects=("freeze", "notify_both"),
        hint="接单人尚无进度（请走无责取消）或静默未满 45 分钟",
        summary="IN_PROGRESS → DISPUTED：发单人失联投诉（免举证）",
    ),
    Trans(
        id="T17",
        kind=TransKind.T,
        from_status=Status.IN_PROGRESS,
        actions=(Action.CANCEL_LOST,),
        to_status=Status.CANCELLED,
        actors=frozenset({Role.PUBLISHER}),
        guard=guard_no_progress_45m,
        effects=("credit_minus_3", "release_inflight", "mark_br33", "notify_both"),
        hint="接单人已产生进度（只能走投诉）或距抢单未满 45 分钟",
        summary="IN_PROGRESS → CANCELLED：失联自助取消（信誉 −3）",
    ),
)

#: **恰好 17 条**，与 PRD 363–383 行逐条对齐。T1 虽然由 `create()` 直接落库
#: （不进状态机），仍保留在本表里，否则「17 条迁移各 1 条正例」无法枚举。
TRANSITIONS: Final[tuple[Trans, ...]] = _TRANSITIONS_LIST


# ---------------------------------------------------------------------------
# 辅助迁移（不占 T 编号，但必须能通过 apply()，否则 5.4 列出的 action 会被误拒）
# ---------------------------------------------------------------------------

def _self_loop(status: Status, action: Action, actors: frozenset[Role], hint: str) -> Trans:
    return Trans(
        id=f"SELF-{status.value}-{action.value}",
        kind=TransKind.SELF_LOOP,
        from_status=status,
        actions=(action,),
        to_status=status,
        actors=actors,
        guard=guard_runner_or_publisher_of_order,
        effects=("notify_peer",),
        hint=hint,
        summary=f"{status.value} → {status.value}：{action.value}（自环，只写事件）",
    )


def guard_runner_or_publisher_of_order(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """双方之一（协商取消的申请/拒绝）。"""
    return _party_guard(ctx, actor)


def guard_urge(
    ctx: OrderCtx,
    actor: Actor,
    now: datetime.datetime,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
    action: Action | None = None,
) -> bool:
    """`URGE`（催单，FR-FULFILL-05）：**仅发单人** + 每单最多 `max_urge_per_order` 次。

    次数从 `OrderCtx.urge_count` 取（真实链路由 repo 用
    `SELECT count(*) FROM order_progress WHERE order_id=? AND action='URGE'` 得出，
    与 T16/T17 的 `probe_progress` 一样走 `order_progress(order_id, seq)` 索引）。
    第 3 次（`urge_count >= 2`）返回 42201，前端据此显示「已催 2 次」并置灰。
    """
    if actor.role is not Role.PUBLISHER or actor.id != ctx.publisher_id:
        return False
    return ctx.urge_count < cfg.max_urge_per_order


def _make_urge() -> Trans:
    """`URGE` 自环：不占 T 编号（PRD 迁移清单只收会改状态的动作）。

    保留在 `ALL_TRANSITIONS` 里的唯一理由：**催单必须落 `order_progress`**。
    它是"发单人是否在骚扰"与"接单人是否该被预警"的举证依据（BR-35 / T-24），
    而 5.4 把它列为 `IN_PROGRESS` 的可执行动作 —— 若拿不到事件载体，
    FR-FULFILL-05 的"每单最多 2 次"就只能靠 Redis 计数，重启即归零。
    """
    return Trans(
        id="SELF-IN_PROGRESS-URGE",
        kind=TransKind.SELF_LOOP,
        from_status=Status.IN_PROGRESS,
        actions=(Action.URGE,),
        to_status=Status.IN_PROGRESS,
        actors=frozenset({Role.PUBLISHER}),
        guard=guard_urge,
        effects=("notify_runner",),
        hint="催单仅限发单人，且每单最多 2 次",
        summary="IN_PROGRESS → IN_PROGRESS：催单（自环，只写事件，不改节点）",
    )


def _make_freeze_evidence(action: Action) -> Trans:
    return Trans(
        id=f"FREEZE-{action.value}",
        kind=TransKind.FREEZE_EVIDENCE,
        from_status=Status.DISPUTED,
        actions=(action,),
        to_status=Status.DISPUTED,
        actors=frozenset({Role.RUNNER}),
        guard=guard_runner_only,
        effects=(),
        hint="冻结期只有接单人可以补证据，且不改订单状态",
        summary=f"DISPUTED → DISPUTED：补证据 {action.value}（补丁 B，不改 status）",
    )


def _make_force_close() -> tuple[Trans, ...]:
    """BR-32 强制关闭：唯一线上事故兜底（D6a 三界面之一）。

    对**所有非终态**开放（含 PENDING/DELIVERED/COMPLETED/DISPUTED）——
    事故兜底如果还挑状态，就兜不住事故。
    """
    out: list[Trans] = []
    for status in (
        Status.PENDING,
        Status.IN_PROGRESS,
        Status.DELIVERED,
        Status.COMPLETED,
        Status.DISPUTED,
    ):
        out.append(
            Trans(
                id=f"FORCE-CLOSE-{status.value}",
                kind=TransKind.ADMIN_OVERRIDE,
                from_status=status,
                actions=(Action.FORCE_CLOSE,),
                to_status=Status.CANCELLED,
                actors=frozenset({Role.ADMIN}),
                guard=guard_admin_force_close,
                effects=("release_inflight", "notify_both", "write_audit"),
                hint="仅管理员可强制关闭",
                summary=f"{status.value} → CANCELLED：管理员强制关闭（BR-32）",
            )
        )
    return tuple(out)


#: 补丁 B（7.4 唯一定义点）：冻结期间只补证据、不改 status。
POST_FREEZE_EVIDENCE: Final[frozenset[tuple[Status, Action]]] = frozenset(
    (Status.DISPUTED, action)
    for action in (Action.PICKED_UP, Action.ON_THE_WAY, Action.DELIVER)
)

AUXILIARY_TRANSITIONS: Final[tuple[Trans, ...]] = (
    # 5.4：IN_PROGRESS 可执行的协商取消两个自环动作（PRD 未给 T 编号）
    _self_loop(
        Status.IN_PROGRESS,
        Action.REQUEST_CANCEL,
        frozenset({Role.PUBLISHER, Role.RUNNER}),
        "只有订单双方能发起协商取消",
    ),
    _self_loop(
        Status.IN_PROGRESS,
        Action.REJECT_CANCEL,
        frozenset({Role.PUBLISHER, Role.RUNNER}),
        "只有订单双方能处理协商取消",
    ),
    # 催单自环（FR-FULFILL-05）：唯一目的是把"催了几次"落成事实
    _make_urge(),
    *(_make_freeze_evidence(action) for action in (Action.PICKED_UP, Action.ON_THE_WAY, Action.DELIVER)),
    *_make_force_close(),
)

#: 全部可执行迁移 = 17 条 + 辅助动作。
ALL_TRANSITIONS: Final[tuple[Trans, ...]] = TRANSITIONS + AUXILIARY_TRANSITIONS


# ---------------------------------------------------------------------------
# 索引与查询
# ---------------------------------------------------------------------------
def _build_index(
    transitions: Sequence[Trans],
) -> dict[tuple[str | None, Action], tuple[Trans, ...]]:
    idx: dict[tuple[str | None, Action], list[Trans]] = {}
    for trans in transitions:
        key_status = trans.from_status.value if isinstance(trans.from_status, Status) else None
        for action in trans.actions:
            idx.setdefault((key_status, action), []).append(trans)
    return {k: tuple(v) for k, v in idx.items()}


_TRANSITION_INDEX: Final[dict[tuple[str | None, Action], tuple[Trans, ...]]] = _build_index(
    ALL_TRANSITIONS
)

#: 合法 `(from_status, action)` 键集合。非法迁移矩阵测试直接用它算期望。
LEGAL_TRANSITION_KEYS: Final[frozenset[tuple[str | None, Action]]] = frozenset(
    _TRANSITION_INDEX
)


def transition_candidates(
    status: str | Status | None, action: str | Action
) -> tuple[Trans, ...]:
    """取该 `(状态, 动作)` 的候选迁移（T13/T14 会返回 2 条，按守卫区分）。

    未知 action（不在 19 个枚举里）返回空元组 —— 调用方应抛 `42201`，
    而不是 `KeyError`（用户传了脏数据不该变成 500）。
    """
    try:
        act = action if isinstance(action, Action) else Action(action)
    except ValueError:
        return ()
    key_status: str | None = None
    if status is not None:
        key_status = status.value if isinstance(status, Status) else str(status)
    return _TRANSITION_INDEX.get((key_status, act), ())


def illegal_action(
    status: str | Status, action: str | Action, *, detail: str | None = None
) -> ApiError:
    """`42201`：状态机非法迁移（D3 可识别错误，前端据此显示"该单已不可接"）。"""
    st = status.value if isinstance(status, Status) else str(status)
    ac = action.value if isinstance(action, Action) else str(action)
    message = detail or f"非法迁移 {st} -/-> {ac}"
    return ApiError(422, ErrCode.ILLEGAL_TRANSITION, message)


def available_actions(order: OrderCtx, actor: Actor) -> tuple[Action, ...]:
    """`available_actions[]`（FR-CENTER-05）：按「状态 × 角色」算，前端只渲染。

    只做**角色层**过滤，不跑守卫（守卫要 now/阈值，且部分守卫会查库）。
    因此返回值是「按钮可以显示」的集合，点了之后仍可能被守卫拒绝并返回 42201 ——
    这是刻意的：把「能不能点」和「点了会不会成功」分开，避免为渲染去跑 19 个守卫。
    """
    out: list[Action] = []
    for (status_key, action), candidates in _TRANSITION_INDEX.items():
        if status_key != order.status:
            continue
        if any(actor.role in trans.actors for trans in candidates):
            out.append(action)
    return tuple(sorted(out, key=lambda a: a.value))


#: 会改 `orders.status` 的 action 集合（投影对账：`status = last(这些事件).to_status`）。
STATUS_MUTATING: Final[frozenset[Action]] = frozenset(
    action
    for trans in ALL_TRANSITIONS
    if trans.mutates_status
    for action in trans.actions
)


# ---------------------------------------------------------------------------
# 进度口径：probe_progress（唯一实现）
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProgressFact:
    """一条 `order_progress` 事实的最小投影（只取口径需要的列）。"""

    action: str
    actor_id: int | None
    created_at: datetime.datetime


def probe_progress(
    facts: Iterable[ProgressFact],
    *,
    runner_id: int | None,
    accepted_at: datetime.datetime | None,
) -> tuple[bool, datetime.datetime | None]:
    """「是否已有接单人进度」+「静默起点」。T16 / T17 / `silent_since` 共用同一个口径。

    等价的 SQL（T-24 用，索引 `order_progress(order_id, seq)`，`ORDER BY seq DESC LIMIT 1`）::

        SELECT action, created_at FROM order_progress
        WHERE order_id = :oid
          AND actor_id = :runner_id
          AND action IN ('PICKED_UP','ON_THE_WAY','DELIVER','NOTE')
        ORDER BY seq DESC LIMIT 1

    两个关键过滤（7.4 反例）：
    - `actor_id == runner_id`：发单人的 `NOTE`（催单）不算进度；
    - `action ∈ RUNNER_EVIDENCE`：`URGE` / `REQUEST_CANCEL` / `REJECT_CANCEL` 都不算。
      **不要**写成「action ∉ {CREATE, ACCEPT}」—— 那个口径过宽，
      会让发单人第 10 分钟催一次单就永久关死自己的 T17 出口。
    """
    last: datetime.datetime | None = None
    for fact in facts:
        action = fact.action.value if isinstance(fact.action, Action) else str(fact.action)
        if action not in RUNNER_EVIDENCE:
            continue
        if runner_id is None or fact.actor_id is None or fact.actor_id != runner_id:
            continue
        if last is None or fact.created_at >= last:
            last = fact.created_at
    if last is None:
        return False, accepted_at
    return True, last


def project_status(events: Iterable[tuple[str | Action, str]]) -> str | None:
    """投影对账：`status = last(STATUS_MUTATING 事件).to_status`（5.4）。

    参数是 `(action, to_status)` 序列（按 `seq` 升序）。用于：
    ① 单测断言随机 1000 条序列的聚合一致；② T-30 的对账脚本。
    """
    result: str | None = None
    for action, to_status in events:
        act = action.value if isinstance(action, Action) else str(action)
        try:
            if Action(act) in STATUS_MUTATING:
                result = to_status
        except ValueError:
            continue
    return result


def resolve_status(trans: Trans, ctx: OrderCtx) -> Status:
    """把 `Trans.to_status` 解析成具体状态（处理 T14 的 `PRE_DISPUTE` 哨兵）。"""
    target = trans.to_status
    if target == PRE_DISPUTE:
        pre = ctx.pre_dispute_status
        if pre is None:
            raise ApiError(
                422,
                ErrCode.PRE_DISPUTE_STATUS_MISSING,
                "订单缺少投诉前状态（pre_dispute_status），无法回滚，请人工处理",
            )
        try:
            return Status(pre)
        except ValueError as exc:  # pragma: no cover - 只可能来自脏数据
            raise ApiError(
                422,
                ErrCode.PRE_DISPUTE_STATUS_MISSING,
                f"投诉前状态非法：{pre}",
            ) from exc
    assert isinstance(target, Status)
    return target


# ---------------------------------------------------------------------------
# apply()：唯一的状态写入出口
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TransitionPayload:
    """一次迁移的附加载荷。

    `note` 进 `order_progress.note`；`images` 是送达凭证；
    `reason` 是取消/关闭原因（写 `orders.cancel_reason`）；
    `extra` 留给上层带业务字段（不落库，只给 effects 用）。
    """

    note: str | None = None
    images: tuple[str, ...] = ()
    reason: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """待追加的 `order_progress` 事实。`seq` 由 repo 分配（DB 权威，勿在应用层算）。"""

    order_id: int
    action: Action
    from_status: str
    to_status: str
    actor_id: int | None
    actor_role: Role
    note: str | None = None
    images: tuple[str, ...] = ()


@runtime_checkable
class OrderRepo(Protocol):
    """状态机依赖的最小仓储协议（T-13/T-20 用 SQLAlchemy 实现）。

    刻意只开两个方法：
    - `append_progress`：**先进事实**（append-only，永不 UPDATE/DELETE）；
    - `cas_update_status`：**再改聚合**，返回受影响行数（0 行 = 并发失败）。
    两个都在**同一个调用方事务**里（K-09：一个请求一个事务），
    repo 内部不许 commit。
    """

    def append_progress(self, event: ProgressEvent, *, created_at: datetime.datetime) -> int:
        """追加事实并返回分配到的 `seq`。"""
        ...

    def cas_update_status(
        self,
        *,
        order_id: int,
        expected_status: str,
        to_status: str,
        patch: Mapping[str, Any],
    ) -> int:
        """`UPDATE orders SET status=:to, version=version+1, **patch WHERE id=:id AND status=:expected`。

        返回受影响行数：1 成功、0 表示状态已被别人改掉（抛 `40905`）。
        """
        ...


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """`apply()` 的返回值。上层用它写通知、拼响应。"""

    transition_id: str
    action: Action
    from_status: str
    to_status: str
    seq: int
    status_patch: Mapping[str, Any]
    effects: tuple[str, ...]
    progress: ProgressEvent


def status_patch(
    trans: Trans,
    ctx: OrderCtx,
    action: Action,
    to_status: Status,
    now: datetime.datetime,
    cfg: Thresholds,
    payload: TransitionPayload,
) -> dict[str, Any]:
    """时间戳与挂起态的**唯一写入口**（7.4 `_timestamps` + `restore_status` 的合并）。

    规则（一次事务里同源同刻，禁止 service 层各写各的）：
    - `ACCEPT`  → `accepted_at = now`
    - `DELIVER` → `delivered_at = now` **且** `auto_confirm_due_at = now + 24h`
    - `CONFIRM` / `AUTO_CONFIRM` → `completed_at = now`；后者另置 `auto_confirmed=true`
    - `DISPUTE_OPENED` → `pre_dispute_status = 迁移前状态`（BR-35 挂起态）
    - **出 DISPUTED 一律先清空** `pre_dispute_status` 与 `auto_confirm_due_at`；
      仅当 T14 回 `DELIVERED` 时把 `auto_confirm_due_at` 覆盖为 `now + 24h`（重新起算）
    - 所有取消类 → `cancel_reason = payload.reason`
    - `delivered_at` **永不改写**（K-12：履约时长的唯一基准）

    ## 为什么"出 DISPUTED 默认清空"是必需而不是洁癖

    7.4 的 `restore_status` 默认 patch 就是 `{"pre_dispute_status": None,
    "auto_confirm_due_at": None}`。只"设"不"清"会留下两个隐患：
    ① T13（PRE=IN_PROGRESS）与 T14（PRE=COMPLETED）回到一个**不需要 24h 计时**的状态，
       却把投诉前的旧 `auto_confirm_due_at` 带出来 —— 一旦该值已过期，
       任何"顺手扫一遍到期单"的实现都会把一张进行中的单判成已完成；
    ② `pre_dispute_status` 在 5.2 里写明"仅 `status='DISPUTED'` 时有意义"，
       不清就是脏数据，而下一次进 DISPUTED 时会被覆盖 —— 谁也不敢保证覆盖前
       没人读过它（RSK-9 的整条风险就是"读到错的 pre 状态"）。

    因此 `ARBITRATE_FREE` / `ARBITRATE_LIABLE` **无条件**置空这两个字段，
    只有 T14 把 `auto_confirm_due_at` 显式改写成新值。
    """
    patch: dict[str, Any] = {}
    if trans.kind is TransKind.FREEZE_EVIDENCE:
        return patch

    if action is Action.ACCEPT:
        patch["accepted_at"] = now
    elif action is Action.DELIVER:
        patch["delivered_at"] = now
        patch["auto_confirm_due_at"] = now + cfg.auto_confirm_delta
    elif action in (Action.CONFIRM, Action.AUTO_CONFIRM):
        patch["completed_at"] = now
        if action is Action.AUTO_CONFIRM:
            patch["auto_confirmed"] = True

    if action is Action.DISPUTE_OPENED:
        # 挂起态语义：写「投诉前状态」，裁决无责时按它回滚（T13/T14）
        patch["pre_dispute_status"] = ctx.status
    elif action in (Action.ARBITRATE_FREE, Action.ARBITRATE_LIABLE):
        # 出 DISPUTED：默认清空（对齐 7.4 restore_status 的默认 patch）
        patch["pre_dispute_status"] = None
        patch["auto_confirm_due_at"] = None
        if action is Action.ARBITRATE_FREE and to_status is Status.DELIVERED:
            # T14 回 DELIVERED：重新起算 24h。**不动 delivered_at**（K-12）
            patch["auto_confirm_due_at"] = now + cfg.auto_confirm_delta
    elif action is Action.FORCE_CLOSE and ctx.status == Status.DISPUTED.value:
        # 管理员从 DISPUTED 强制关闭也是"出 DISPUTED"的一条路径（BR-32 事故兜底）。
        # 漏掉它会得到一张 status=CANCELLED 却仍带 pre_dispute_status 的单 ——
        # 5.2 明确该字段"仅 status='DISPUTED' 时有意义"。
        patch["pre_dispute_status"] = None
        patch["auto_confirm_due_at"] = None

    if action in (
        Action.CANCEL,
        Action.CANCEL_LOST,
        Action.ARBITRATE_LIABLE,
        Action.FORCE_CLOSE,
    ) and payload.reason:
        patch["cancel_reason"] = payload.reason

    return patch


def _coerce_payload(payload: TransitionPayload | Mapping[str, Any] | None) -> TransitionPayload:
    if payload is None:
        return TransitionPayload()
    if isinstance(payload, TransitionPayload):
        return payload
    images = payload.get("images") or ()
    return TransitionPayload(
        note=payload.get("note"),
        images=tuple(images),
        reason=payload.get("reason"),
        extra={k: v for k, v in payload.items() if k not in {"note", "images", "reason"}},
    )


def apply(
    repo: OrderRepo,
    order: OrderCtx,
    action: Action | str,
    actor: Actor,
    payload: TransitionPayload | Mapping[str, Any] | None = None,
    *,
    now: datetime.datetime | None = None,
    cfg: Thresholds = DEFAULT_THRESHOLDS,
) -> ApplyResult:
    """统一事务出口（5.4 / 7.4）—— **禁止在 service 里直接改 orders.status**。

    顺序不可调换：
    1. 查表：`(status, action)` 无候选 → `42201`（非法迁移，D3 可识别）；
    2. 角色：actor 的 role 不在候选的 `actors` 里 → `40310`（非本人/无权）；
    3. 挂起态预检查：`DISPUTED + ARBITRATE_FREE` 且 PRE 缺失 → `42202`（见下）；
    4. 守卫：角色合法但守卫返回 False → `42201`（条件不满足）；
    5. **先 append `order_progress` 事实**（append-only）；
    6. 再 CAS `UPDATE orders`，0 行 → `40905`（状态已被其他操作改变）。

    第 2、3 步分开抛不同错误码，是因为「你不是这单的人」和「时候未到」
    对用户是两回事：前者要挡住越权，后者要提示再等多久。

    ## 仲裁前先查挂起态（F4）—— 但**必须让角色校验先过**（P3）

    `DISPUTED + ARBITRATE_FREE` 若 `pre_dispute_status` 为空，要抛 `42202`
    而不是让 T13/T14 的守卫各自失败（两个守卫都要求 PRE 是具体值，PRE=None 时都返回
    False，`apply()` 会退化成 `42201`，且 hint 取候选列表第一个（T13）的
    「投诉前状态不是进行中，应走 T14」—— 把管理员引向一个同样走不通的方向）。

    但这段预检查**必须放在角色校验之后**、守卫循环之前：
    ① 否则「无权操作」会被误报成「数据异常」，管理员排障方向被带偏；
    ② 否则越权调用方（RUNNER / PUBLISHER / SYSTEM）能从错误码反推出
       「该单挂起态缺失」这一内部状态 —— 属于信息泄露。
    顺序因此是：查表 → 角色 → **挂起态预检查** → 守卫。
    """
    moment = default_clock.to_utc(now) if now is not None else default_clock.now()
    try:
        act = action if isinstance(action, Action) else Action(action)
    except ValueError as exc:
        raise illegal_action(order.status, action) from exc

    candidates = transition_candidates(order.status, act)
    if not candidates:
        raise illegal_action(order.status, act)

    role_ok = tuple(t for t in candidates if actor.role in t.actors)
    if not role_ok:
        raise ApiError(
            403,
            ErrCode.ORDER_NOT_YOURS,
            f"当前身份（{actor.role.value}）无权执行 {act.value}",
        )

    # 挂起态缺失 → 42202（"数据有问题，需人工核对"），不是 42201（"操作非法"）。
    # 位置：角色校验之后（越权先拒，不泄露内部状态）、守卫循环之前（避免被 T13 的 hint 误导）。
    if (
        act is Action.ARBITRATE_FREE
        and order.status == Status.DISPUTED.value
        and order.pre_dispute_status is None
    ):
        raise ApiError(
            422,
            ErrCode.PRE_DISPUTE_STATUS_MISSING,
            "挂起态状态缺失，请人工核对",
        )

    chosen: Trans | None = None
    for candidate in role_ok:
        if candidate.guard(order, actor, moment, cfg, act):
            chosen = candidate
            break
    if chosen is None:
        raise illegal_action(
            order.status,
            act,
            detail=role_ok[0].hint or f"非法迁移 {order.status} -/-> {act.value}",
        )

    target = resolve_status(chosen, order)
    body = _coerce_payload(payload)
    patch = status_patch(chosen, order, act, target, moment, cfg, body)

    event = ProgressEvent(
        order_id=order.id,
        action=act,
        from_status=order.status,
        to_status=target.value,
        actor_id=actor.id,
        actor_role=actor.role,
        note=body.note,
        images=body.images,
    )
    seq = repo.append_progress(event, created_at=moment)

    affected = repo.cas_update_status(
        order_id=order.id,
        expected_status=order.status,
        to_status=target.value,
        patch=patch,
    )
    if affected != 1:
        raise ApiError(
            409,
            ErrCode.STATUS_CHANGED_CONCURRENTLY,
            "订单状态已被其他操作改变，请刷新后重试",
        )

    return ApplyResult(
        transition_id=chosen.id,
        action=act,
        from_status=order.status,
        to_status=target.value,
        seq=seq,
        status_patch=patch,
        effects=chosen.effects,
        progress=event,
    )


# ---------------------------------------------------------------------------
# 自检：import 期跑（纯计算，无 I/O），把「编号漏了/重了」变成启动即失败
# ---------------------------------------------------------------------------
def _self_check() -> None:
    if len(TRANSITIONS) != 17:
        raise RuntimeError(f"TRANSITIONS 必须是 17 条，实际 {len(TRANSITIONS)} 条")
    ids = [t.id for t in TRANSITIONS]
    expected = [f"T{i}" for i in range(1, 18)]
    if ids != expected:
        raise RuntimeError(f"迁移编号必须是 T1..T17 顺序排列，实际 {ids}")
    if len(set(PROGRESS_ACTIONS)) != 19:
        raise RuntimeError("Action 必须是 19 个（5.4 的 18 个 + 补入的 URGE）")
    for trans in TRANSITIONS:
        if trans.kind is not TransKind.T:
            raise RuntimeError(f"{trans.id} 的 kind 必须是 T")
    if frozenset(
        (t.from_status, t.actions[0])
        for t in AUXILIARY_TRANSITIONS
        if t.kind is TransKind.FREEZE_EVIDENCE
    ) != POST_FREEZE_EVIDENCE:
        raise RuntimeError("POST_FREEZE_EVIDENCE 与 7.4 不一致")
    if not _TRANSITION_INDEX:
        raise RuntimeError("迁移索引为空")


_self_check()
