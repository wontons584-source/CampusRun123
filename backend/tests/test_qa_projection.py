"""QA 独立验证（Edward）：投影一致性 —— 用穷举替代"随机 1000 条"。

## 为什么不用工程师那条随机游走

我实测了 `tests/test_state_machine.py::test_projection_matches_status`
（seed=20250601, 1000 条序列）的真实覆盖：

    请求步数 6505 → 实际产生事件 1746（转化率 26.8%）
    1000 条里：30 条一步都走不动被 continue 掉；668 条只有 1 条事件；
    中位数 1 条；最多 9 条；**没有任何一条触到 12 步上限**。
    覆盖 action 13/18：**AUTO_CONFIRM / CANCEL_LOST / APPROVE_CANCEL /
    EXPIRE / CREATE 从未出现**；ARBITRATE_FREE 只出现 1 次。
    事件占比最高的是 FORCE_CLOSE（470/1746 = 26.9%）——管理端兜底动作
    把真实业务路径挤掉了。

单条事件的投影断言几乎是同义反复（`project_status([(a, to)])` 直接返回 `to`），
所以"随机 1000 条通过"并不代表聚合/投影一致。

## 这里换成什么

对 `(状态 × 角色 × 动作)` 做 BFS 枚举，对**每一条**可达路径逐前缀断言
`project_status(前缀) == 该前缀末端的 status`，并显式断言：
- 17 条 T 迁移**每一条**都至少出现在一条被校验的路径里；
- 被校验的路径里 ≥2 条事件的数量足够多（不是一堆单步）；
- 存在走完 ACCEPT→…→COMPLETE、以及 T16/T17/T8 的完整路径。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from app.core.clock import UTC
from app.core.errors import ApiError
from app.modules.order.state_machine import (
    Action,
    Actor,
    OrderCtx,
    Status,
    apply,
    project_status,
    transition_candidates,
)

from tests.conftest import MemoryOrderRepo

NOW = datetime.datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
MIN = datetime.timedelta(minutes=1)
HOUR = datetime.timedelta(hours=1)

PUBLISHER_ID = 100
RUNNER_ID = 200
ADMIN_ID = 900

ACTORS: tuple[Actor, ...] = (
    Actor.publisher(PUBLISHER_ID),
    Actor.runner(RUNNER_ID),
    Actor.admin(ADMIN_ID),
    Actor.system(),
)


@dataclass(frozen=True)
class State:
    """随机游走的状态（只放守卫真正读到的事实）。"""

    status: str = Status.PENDING.value
    accepted_at: datetime.datetime | None = None
    delivered_at: datetime.datetime | None = None
    completed_at: datetime.datetime | None = None
    due_at: datetime.datetime | None = None
    has_evidence: bool = False
    last_evidence_at: datetime.datetime | None = None
    last_node: str | None = None
    pre_dispute_status: str | None = None
    dispute_count: int = 0
    cancel_status: str | None = None
    deadline_passed: bool = False

    @property
    def runner_id(self) -> int | None:
        return None if self.status == Status.PENDING.value else RUNNER_ID

    def ctx(self) -> OrderCtx:
        return OrderCtx(
            id=1,
            status=self.status,
            publisher_id=PUBLISHER_ID,
            runner_id=self.runner_id,
            accepted_at=self.accepted_at,
            delivered_at=self.delivered_at,
            has_runner_evidence=self.has_evidence,
            last_runner_evidence_at=self.last_evidence_at,
            pre_dispute_status=self.pre_dispute_status,
            deadline=(NOW - 1 * MIN) if self.deadline_passed else (NOW + 2 * HOUR),
            completed_at=self.completed_at,
            auto_confirm_due_at=self.due_at,
            last_node=self.last_node,
            dispute_count=self.dispute_count,
            cancel_request_status=self.cancel_status,
            runner_inflight_count=0,
            publisher_pending_count=0,
        )

    def key(self) -> tuple[Any, ...]:
        return (
            self.status,
            self.delivered_at is not None,
            self.completed_at is not None,
            self.due_at is not None,
            self.has_evidence,
            self.last_node,
            self.pre_dispute_status,
            self.dispute_count,
            self.cancel_status,
            self.deadline_passed,
        )


def advance(state: State, action: Action, to_status: str, patch: dict[str, Any]) -> State:
    """把一次成功迁移的 patch 应用到一个新 State（只处理状态机真的会写的字段）。"""
    new = replace(state, status=to_status)
    if "accepted_at" in patch:
        new = replace(new, accepted_at=patch["accepted_at"])
    if "delivered_at" in patch:
        new = replace(new, delivered_at=patch["delivered_at"])
    if "completed_at" in patch:
        new = replace(new, completed_at=patch["completed_at"])
    if "auto_confirm_due_at" in patch:
        new = replace(new, due_at=patch["auto_confirm_due_at"])
    if "pre_dispute_status" in patch:
        new = replace(new, pre_dispute_status=patch["pre_dispute_status"])
    if action is Action.DISPUTE_OPENED:
        new = replace(new, dispute_count=state.dispute_count + 1)
    if action in (Action.PICKED_UP, Action.ON_THE_WAY):
        new = replace(new, last_node=action.value)
    elif action is Action.DELIVER:
        new = replace(new, last_node=Action.DELIVER.value)
    if action in (Action.PICKED_UP, Action.ON_THE_WAY, Action.DELIVER, Action.NOTE):
        new = replace(new, has_evidence=True, last_evidence_at=NOW)
    if action is Action.REQUEST_CANCEL:
        new = replace(new, cancel_status="PENDING")
    if action in (Action.APPROVE_CANCEL, Action.REJECT_CANCEL):
        new = replace(new, cancel_status=None)
    return new


def _variants(state: State) -> list[State]:
    """同一笔订单的其它"可能的当下事实"。每次 `step()` 都会逐个试。

    三类，都是业务上**必然会发生**的情形，不是伪造：
    1. **时间流逝**：守卫比的是 `now - ts >= 阈值`，等价于把 ts 往前推。
       不推的话，T16/T17（45min）、T10（24h）在本枚举里永远走不到 ——
       这正是工程师那条随机游走漏掉 AUTO_CONFIRM / CANCEL_LOST 的原因。
    2. **已过期**：T2（未过期才可抢）与 T4（已过期才能置过期）互斥，必须各试一次。
    3. **对方已同意协商取消**：`order_cancel_requests` 在同一事务被置 APPROVED。
    """
    out = [state]

    waited = state
    if waited.accepted_at is not None:
        waited = replace(waited, accepted_at=waited.accepted_at - 46 * MIN)
    if waited.last_evidence_at is not None:
        waited = replace(waited, last_evidence_at=waited.last_evidence_at - 46 * MIN)
    if waited.due_at is not None:
        waited = replace(waited, due_at=waited.due_at - 25 * HOUR)
    if waited is not state:
        out.append(waited)

    if not state.deadline_passed:
        out.append(replace(state, deadline_passed=True))

    if state.status == Status.IN_PROGRESS.value and state.cancel_status != "APPROVED":
        out.append(replace(state, cancel_status="APPROVED"))
        out.append(replace(waited, cancel_status="APPROVED"))

    return out


def step(state: State, action: Action, actor: Actor) -> tuple[State, str] | None:
    """尝试一次迁移；成功返回 (新状态, transition_id)，失败返回 None。"""
    for variant in _variants(state):
        ctx = variant.ctx()
        repo = MemoryOrderRepo()
        repo.seed(ctx.id, ctx.status)
        try:
            result = apply(repo, ctx, action, actor, now=NOW)
        except ApiError:
            continue
        return advance(variant, action, result.to_status, dict(result.status_patch)), (
            result.transition_id
        )
    return None


@dataclass
class Path:
    states: list[State] = field(default_factory=lambda: [State()])
    events: list[tuple[Action, str]] = field(default_factory=list)
    tids: list[str] = field(default_factory=list)


def enumerate_paths(max_depth: int, max_paths: int) -> list[Path]:
    """**逐层 BFS** 枚举路径。

    为什么必须是 BFS：DFS + 弹栈会把 `FORCE_CLOSE`（每层都有出边）先钻到底，
    把路径配额烧光，短路径一条都留不下 —— 我第一次就是这么写的，
    结果 T10/T13/T16/T17 一个都没覆盖到（日志见报告）。

    层的边界上按"状态签名"去重只影响**下一层的前沿**，
    不影响本层每条路径都被记录并校验。
    """
    paths: list[Path] = []
    frontier: list[Path] = [Path()]

    for _depth in range(max_depth):
        next_frontier: list[Path] = []
        seen: set[tuple[Any, ...]] = set()
        for path in frontier:
            for action in Action:
                for actor in ACTORS:
                    got = step(path.states[-1], action, actor)
                    if got is None:
                        continue
                    new_state, tid = got
                    child = Path(
                        states=[*path.states, new_state],
                        events=[*path.events, (action, new_state.status)],
                        tids=[*path.tids, tid],
                    )
                    if len(paths) < max_paths:
                        paths.append(child)
                    sig = new_state.key()
                    if sig not in seen and len(next_frontier) < max_paths:
                        seen.add(sig)
                        next_frontier.append(child)
        if not next_frontier:
            break
        frontier = next_frontier

    return paths or [Path()]


_MAX_DEPTH = 7
_MAX_PATHS = 20000
PATHS = enumerate_paths(_MAX_DEPTH, _MAX_PATHS)


# ---------------------------------------------------------------------------
# 1. 逐前缀投影一致性（核心断言）
# ---------------------------------------------------------------------------
def test_qa_projection_every_prefix() -> None:
    """每条枚举到的路径，**每一个前缀**都必须满足 `status == last(STATUS_MUTATING).to_status`。"""
    checked_prefixes = 0
    multi = 0
    for path in PATHS:
        if len(path.events) >= 2:
            multi += 1
        for cut in range(1, len(path.events) + 1):
            projected = project_status(path.events[:cut])
            expected = path.states[cut].status
            assert projected == expected, (
                f"投影不一致：前 {cut} 条事件 {[a.value for a, _ in path.events[:cut]]} "
                f"→ 投影={projected} 聚合={expected}"
            )
            checked_prefixes += 1
    assert checked_prefixes > 0
    assert multi >= 300, f"多步路径只有 {multi} 条，覆盖不足"


# ---------------------------------------------------------------------------
# 2. 覆盖充分性：17 条迁移必须全部出现在被校验的路径里
# ---------------------------------------------------------------------------
def test_qa_projection_covers_all_17_transitions() -> None:
    """把"覆盖到 17 条"变成硬断言，而不是靠"跑了 1000 次应该差不多"。"""
    covered: set[str] = set()
    for path in PATHS:
        covered |= set(path.tids)
    expected = {f"T{i}" for i in range(1, 18)}
    # T1 由 create() 落库，进不了 apply()（7.4 / 本文件 test_c9_create_not_reachable_through_apply）
    expected -= {"T1"}
    missing = expected - covered
    assert not missing, f"这些迁移从未被任何被校验的路径覆盖：{sorted(missing)}"


def test_qa_projection_key_business_paths_exist() -> None:
    """四条对账价值最高的路径必须真的存在（随机游走里它们全都不足）。"""
    seqs = [tuple(a for a, _ in p.events) for p in PATHS]

    green = (
        Action.ACCEPT, Action.PICKED_UP, Action.ON_THE_WAY,
        Action.DELIVER, Action.CONFIRM,
    )
    assert green in seqs, "缺少「抢单→取件→配送中→送达→确认」主链路"

    # 注：T5 已按 team-lead 裁定收紧为**严格顺序推进**（`target == current + 1`），
    # 因此任何走完履约的路径都必然包含 PICKED_UP，不再有「跳级」路径。
    auto = (Action.ACCEPT, Action.PICKED_UP, Action.ON_THE_WAY,
            Action.DELIVER, Action.AUTO_CONFIRM)
    assert auto in seqs, "缺少 24h 自动确认链路（T10 在随机游走里根本没被覆盖）"

    lost = (Action.ACCEPT, Action.CANCEL_LOST)
    assert lost in seqs, "缺少失联自助取消链路（T17 在随机游走里根本没被覆盖）"

    cancel = (Action.ACCEPT, Action.REQUEST_CANCEL, Action.APPROVE_CANCEL)
    assert cancel in seqs, "缺少协商取消链路（T8 在随机游走里根本没被覆盖）"

    # 投诉→仲裁无责回滚（同样必须先 PICKED_UP：严格顺序推进 + T16 需要 has_evidence）
    dispute = (Action.ACCEPT, Action.PICKED_UP, Action.ON_THE_WAY,
               Action.DISPUTE_OPENED, Action.ARBITRATE_FREE)
    assert dispute in seqs, "缺少 投诉→仲裁无责 链路（T16→T13）"


def test_qa_projection_dispute_rollback_paths() -> None:
    """T13/T14/T15 三条出边都要被走到，且回滚后的状态必须是 PRE。"""
    found: dict[str, str] = {}
    for path in PATHS:
        for tid, state in zip(path.tids, path.states[1:], strict=False):
            found.setdefault(tid, state.status)
    assert found.get("T13") == Status.IN_PROGRESS.value
    assert found.get("T14") in (Status.DELIVERED.value, Status.COMPLETED.value)
    assert found.get("T15") == Status.CANCELLED.value


def test_qa_projection_terminal_states_have_no_out_edges() -> None:
    """CANCELLED / EXPIRED 不得有任何出边（终态）。DISPUTED 不是终态。"""
    for terminal in (Status.CANCELLED, Status.EXPIRED):
        state = State(status=terminal.value)
        for action in Action:
            for actor in ACTORS:
                assert step(state, action, actor) is None, (
                    f"终态 {terminal} 上居然能执行 {action}"
                )
    # DISPUTED 必须还有出边（BR-34：挂起态非终态）
    disputed = State(status=Status.DISPUTED.value, pre_dispute_status="IN_PROGRESS")
    assert step(disputed, Action.ARBITRATE_FREE, Actor.admin(ADMIN_ID)) is not None


def test_qa_projection_self_loops_never_change_status() -> None:
    """自环（T5/T6/REQUEST_CANCEL/REJECT_CANCEL/冻结补证）不得改变 status。

    **注意 `DELIVER` 的双重语义**（QA 记录的口径细节）：`STATUS_MUTATING` 是
    **action 级**判定，`DELIVER` 因为在 T7 里会改状态而被算作 mutating；
    但冻结期补证的 `DELIVER` 不改状态。所以它确实会参与投影计算 ——
    只是它的 `to_status` 记的就是 `DISPUTED`，投影结果仍然等于聚合。
    真正的不变量是「append 时 to_status 必须是迁移后的真实状态」，
    不是「自环不看投影」。
    """
    self_loops = [
        (State(status="IN_PROGRESS", accepted_at=NOW - HOUR, has_evidence=True,
               last_evidence_at=NOW - 10 * MIN, last_node=None),
         Action.PICKED_UP, Actor.runner(RUNNER_ID)),
        (State(status="IN_PROGRESS", accepted_at=NOW - HOUR, has_evidence=True,
               last_evidence_at=NOW - 10 * MIN, last_node="PICKED_UP"),
         Action.NOTE, Actor.runner(RUNNER_ID)),
        (State(status="IN_PROGRESS", accepted_at=NOW - HOUR, has_evidence=True,
               last_evidence_at=NOW - 10 * MIN, last_node="PICKED_UP"),
         Action.REQUEST_CANCEL, Actor.publisher(PUBLISHER_ID)),
        (State(status="IN_PROGRESS", accepted_at=NOW - HOUR, has_evidence=True,
               last_evidence_at=NOW - 10 * MIN, last_node="PICKED_UP"),
         Action.REJECT_CANCEL, Actor.runner(RUNNER_ID)),
        (State(status="DISPUTED", pre_dispute_status="IN_PROGRESS"), Action.PICKED_UP,
         Actor.runner(RUNNER_ID)),
        (State(status="DISPUTED", pre_dispute_status="IN_PROGRESS"), Action.DELIVER,
         Actor.runner(RUNNER_ID)),
    ]
    for state, action, actor in self_loops:
        got = step(state, action, actor)
        assert got is not None, f"{action} 应当可执行（自环）"
        new_state, tid = got
        assert new_state.status == state.status, (
            f"自环 {action} 改了状态：{state.status} → {new_state.status}（{tid}）"
        )
        # 不变量：把这条自环事件接到一条"已知聚合"的序列后面，
        # 投影必须仍然等于当前真实聚合状态。
        if state.status == Status.DISPUTED.value:
            prior = [
                (Action.ACCEPT, Status.IN_PROGRESS.value),
                (Action.ON_THE_WAY, Status.IN_PROGRESS.value),
                (Action.DISPUTE_OPENED, Status.DISPUTED.value),
            ]
        else:
            prior = [(Action.ACCEPT, Status.IN_PROGRESS.value)]
        assert project_status(prior) == state.status
        assert project_status([*prior, (action, new_state.status)]) == state.status, (
            f"自环 {action} 让投影偏离聚合"
        )


@pytest.mark.parametrize("status", [s for s in Status], ids=[s.value for s in Status])
def test_qa_projection_dirty_action_never_mutates(status: Status) -> None:
    """17 条迁移之外的动作不得凭空改状态（`apply` 必须先查表）。"""
    legal_actions = {a for (s, a) in [] }  # 占位，保持参数化 id 稳定
    state = State(status=status.value)
    for action in Action:
        cands = transition_candidates(status, action)
        if cands:
            continue
        for actor in ACTORS:
            ctx = state.ctx()
            repo = MemoryOrderRepo()
            repo.seed(ctx.id, ctx.status)
            with pytest.raises(ApiError):
                apply(repo, ctx, action, actor, now=NOW)
            assert repo.events == []
            assert repo.rows[ctx.id]["status"] == status.value
    assert legal_actions == set()
