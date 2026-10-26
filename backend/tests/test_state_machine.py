"""状态机内核单测（T-12，验收项 D3 / 迁移 T1–T17）。

覆盖清单（逐条对应任务书第六节 + T-12 完成判据）：

1. `test_transition_positive`      —— 17 条迁移各 1 条正例（用例名带 T 编号）
2. `test_illegal_transition_matrix` —— 7 状态 × 18 action 全笛卡尔积，非法键返 42201
3. `test_t16_t17_mutual_exclusion`  —— T16/T17 守卫硬互斥（4 例）
4. `test_silent_threshold_boundary` —— 44:59 拒 / 45:00 过（注入 now，不 sleep）
5. `test_runner_evidence_*`         —— 口径反例 3 条（防回归，7.4 的反例）
6. `test_runner_cannot_call_publisher_actions` —— 接单人自调 → 40310
7. `test_t13_t14_restore`           —— T13 回 IN_PROGRESS、T14 回 pre_dispute_status
8. `test_cas_conflict_*`            —— CAS 0 行 → 40905（并发互斥）
9. `test_projection_matches_status` —— 随机 1000 条事件序列的投影一致性
10. `test_transition_table_integrity` —— 17 条编号、18 个 action、白名单常量

**全部用例零外部依赖**：不连库、不连 Redis、不 sleep。
"""

from __future__ import annotations

import datetime
import random
from dataclasses import dataclass, field
from typing import Any

import pytest
from app.core.clock import UTC
from app.core.errors import ApiError, ErrCode
from app.models.order import PROGRESS_ACTIONS as DB_PROGRESS_ACTIONS
from app.modules.order import state_machine as sm
from app.modules.order.state_machine import (
    DEFAULT_THRESHOLDS,
    POST_FREEZE_EVIDENCE,
    PRE_DISPUTE,
    RUNNER_EVIDENCE,
    STATUS_MUTATING,
    TRANSITIONS,
    Action,
    Actor,
    OrderCtx,
    ProgressFact,
    Role,
    Status,
    Thresholds,
    TransitionPayload,
    apply,
    illegal_action,
    probe_progress,
    project_status,
    transition_candidates,
)

from tests.conftest import BASE_NOW, MemoryOrderRepo

NOW = BASE_NOW
MIN = datetime.timedelta(minutes=1)
HOUR = datetime.timedelta(hours=1)


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------
def ctx(**overrides: Any) -> OrderCtx:
    """构造 `OrderCtx`：默认是一个「有进度、静默很久、未过期」的进行中订单。

    默认值刻意设成「守卫最容易通过」的那一侧，用例只 override 关心的字段，
    这样每条用例的差异一眼可见（而不是埋在 20 个字段里）。
    """
    base: dict[str, Any] = {
        "id": 1,
        "status": Status.IN_PROGRESS.value,
        "publisher_id": 100,
        "runner_id": 200,
        "accepted_at": NOW - 3 * HOUR,
        "delivered_at": None,
        "has_runner_evidence": True,
        "last_runner_evidence_at": NOW - 50 * MIN,
        "pre_dispute_status": None,
        "deadline": NOW + 2 * HOUR,
        "completed_at": None,
        "auto_confirm_due_at": None,
        "last_node": "ON_THE_WAY",
        "dispute_count": 0,
        "cancel_request_status": None,
        "runner_inflight_count": 0,
        "publisher_pending_count": 0,
    }
    base.update(overrides)
    return OrderCtx(**base)


PUBLISHER = Actor.publisher(100)
RUNNER = Actor.runner(200)
ADMIN = Actor.admin(900)
SYSTEM = Actor.system()


@dataclass
class Case:
    """一条迁移的正例。"""

    tid: str
    order: OrderCtx
    actor: Actor
    action: Action
    expected_status: str
    payload: TransitionPayload | None = None
    note: str = ""


def _positive_cases() -> list[Case]:
    """17 条迁移的合法输入（编号顺序与 PRD 一致，便于人工对照）。"""
    return [
        # T1 特殊：— → PENDING，由 create() 直接落库，不进 apply()（7.4）。
        Case(
            tid="T1",
            order=ctx(status=Status.PENDING.value, runner_id=None, has_runner_evidence=False,
                      last_runner_evidence_at=None, accepted_at=None, publisher_pending_count=1),
            actor=PUBLISHER,
            action=Action.CREATE,
            expected_status=Status.PENDING.value,
            note="create() 直落库；此处断言定义与守卫",
        ),
        Case("T2", ctx(status=Status.PENDING.value, runner_id=None, accepted_at=None,
                       has_runner_evidence=False, last_runner_evidence_at=None),
             RUNNER, Action.ACCEPT, Status.IN_PROGRESS.value),
        Case("T3", ctx(status=Status.PENDING.value, runner_id=None, accepted_at=None,
                       has_runner_evidence=False, last_runner_evidence_at=None),
             PUBLISHER, Action.CANCEL, Status.CANCELLED.value,
             TransitionPayload(reason="不想拿了")),
        Case("T4", ctx(status=Status.PENDING.value, runner_id=None, accepted_at=None,
                       has_runner_evidence=False, last_runner_evidence_at=None,
                       deadline=NOW - MIN),
             SYSTEM, Action.EXPIRE, Status.EXPIRED.value),
        Case("T5", ctx(last_node=None), RUNNER, Action.PICKED_UP, Status.IN_PROGRESS.value),
        Case("T5", ctx(last_node="PICKED_UP"), RUNNER, Action.ON_THE_WAY,
             Status.IN_PROGRESS.value),
        Case("T6", ctx(last_node="PICKED_UP"), RUNNER, Action.NOTE, Status.IN_PROGRESS.value,
             TransitionPayload(note="驿站排队，10 分钟后到")),
        Case("T7", ctx(last_node="ON_THE_WAY"), RUNNER, Action.DELIVER,
             Status.DELIVERED.value, TransitionPayload(images=("k/1.jpg",))),
        Case("T8", ctx(cancel_request_status="APPROVED"), RUNNER, Action.APPROVE_CANCEL,
             Status.CANCELLED.value),
        Case("T9", ctx(status=Status.DELIVERED.value, delivered_at=NOW - HOUR,
                       auto_confirm_due_at=NOW + 23 * HOUR),
             PUBLISHER, Action.CONFIRM, Status.COMPLETED.value),
        Case("T10", ctx(status=Status.DELIVERED.value, delivered_at=NOW - 25 * HOUR,
                        auto_confirm_due_at=NOW - HOUR),
             SYSTEM, Action.AUTO_CONFIRM, Status.COMPLETED.value),
        Case("T11", ctx(status=Status.DELIVERED.value, delivered_at=NOW - HOUR),
             PUBLISHER, Action.DISPUTE_OPENED, Status.DISPUTED.value),
        Case("T12", ctx(status=Status.COMPLETED.value, completed_at=NOW - HOUR),
             RUNNER, Action.DISPUTE_OPENED, Status.DISPUTED.value),
        Case("T13", ctx(status=Status.DISPUTED.value, pre_dispute_status=Status.IN_PROGRESS.value),
             ADMIN, Action.ARBITRATE_FREE, Status.IN_PROGRESS.value),
        Case("T14", ctx(status=Status.DISPUTED.value, pre_dispute_status=Status.DELIVERED.value,
                        delivered_at=NOW - 72 * HOUR, auto_confirm_due_at=NOW - 48 * HOUR),
             ADMIN, Action.ARBITRATE_FREE, Status.DELIVERED.value),
        Case("T14", ctx(status=Status.DISPUTED.value, pre_dispute_status=Status.COMPLETED.value,
                        completed_at=NOW - 72 * HOUR),
             ADMIN, Action.ARBITRATE_FREE, Status.COMPLETED.value),
        Case("T16", ctx(has_runner_evidence=True, last_runner_evidence_at=NOW - 46 * MIN),
             PUBLISHER, Action.DISPUTE_OPENED, Status.DISPUTED.value),
        Case("T17", ctx(has_runner_evidence=False, last_runner_evidence_at=None,
                        accepted_at=NOW - 46 * MIN),
             PUBLISHER, Action.CANCEL_LOST, Status.CANCELLED.value,
             TransitionPayload(reason="接单人失联")),
    ]


POSITIVE_CASES = _positive_cases()
POSITIVE_IDS = [f"{c.tid}-{c.action.value}" for c in POSITIVE_CASES]


# ---------------------------------------------------------------------------
# 1. 表结构自检（编号、动作全集、常量）
# ---------------------------------------------------------------------------
def test_transition_table_integrity() -> None:
    """17 条、编号 T1..T17、18 个 action、常量口径。"""
    assert len(TRANSITIONS) == 17
    assert [t.id for t in TRANSITIONS] == [f"T{i}" for i in range(1, 18)]

    # PROGRESS_ACTIONS 与 DB 层 CHECK 约束的取值集合必须完全一致，
    # 否则会出现「状态机放行但 INSERT 被 CHECK 挡住」的 500。
    assert {a.value for a in sm.PROGRESS_ACTIONS} == set(DB_PROGRESS_ACTIONS)
    # 19 = 5.4 正文的 18 个 + 补入的 URGE（催单必须落事实，FR-FULFILL-05）
    assert len(sm.PROGRESS_ACTIONS) == 19

    # 7.4 唯一定义点
    assert RUNNER_EVIDENCE == ("PICKED_UP", "ON_THE_WAY", "DELIVER", "NOTE")
    assert POST_FREEZE_EVIDENCE == {
        (Status.DISPUTED, Action.PICKED_UP),
        (Status.DISPUTED, Action.ON_THE_WAY),
        (Status.DISPUTED, Action.DELIVER),
    }

    # 会改状态的 action 必须包含 13 个（18 − 自环 5 个）
    expected_mutating = {
        Action.CREATE, Action.ACCEPT, Action.CANCEL, Action.EXPIRE, Action.DELIVER,
        Action.APPROVE_CANCEL, Action.CANCEL_LOST, Action.DISPUTE_OPENED,
        Action.CONFIRM, Action.AUTO_CONFIRM, Action.ARBITRATE_FREE,
        Action.ARBITRATE_LIABLE, Action.FORCE_CLOSE,
    }
    assert set(STATUS_MUTATING) == expected_mutating


def test_post_freeze_evidence_does_not_change_status() -> None:
    """补丁 B：冻结期 PICKED_UP/ON_THE_WAY/DELIVER 只写事件，不改 status、不清时间戳。"""
    repo = MemoryOrderRepo()
    repo.seed(1, Status.DISPUTED.value)
    order = ctx(status=Status.DISPUTED.value)
    result = apply(repo, order, Action.PICKED_UP, RUNNER, now=NOW)
    assert result.to_status == Status.DISPUTED.value
    assert dict(result.status_patch) == {}
    assert repo.rows[1]["status"] == Status.DISPUTED.value


# ---------------------------------------------------------------------------
# 2. 17 条迁移正例
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case", POSITIVE_CASES, ids=POSITIVE_IDS)
def test_transition_positive(case: Case) -> None:
    """每条迁移都有一条合法输入能走通，且落到的目标态与 PRD 一致。"""
    if case.tid == "T1":
        # T1 的 from_status 是 None（订单还不存在），create() 直接写库并补一条
        # action=CREATE 的事实，因此不经过 apply()（7.4 明确）。
        candidates = transition_candidates(None, Action.CREATE)
        assert [t.id for t in candidates] == ["T1"]
        assert candidates[0].to_status is Status.PENDING
        assert sm.guard_create(case.order, case.actor, NOW) is True
        # 守卫反例：未认证不能发单
        assert sm.guard_create(case.order, Actor.publisher(100, is_verified=False), NOW) is False
        return

    repo = MemoryOrderRepo()
    repo.seed(case.order.id, case.order.status)
    result = apply(repo, case.order, case.action, case.actor, case.payload, now=NOW)

    assert result.transition_id == case.tid
    assert result.from_status == case.order.status
    assert result.to_status == case.expected_status
    # 先 append 事实，再 CAS 改聚合 —— 两者必须都发生
    assert len(repo.events) == 1
    assert repo.rows[case.order.id]["status"] == case.expected_status
    assert result.progress.to_status == case.expected_status
    assert result.progress.actor_role is case.actor.role


def test_t7_writes_delivered_and_due_at_together() -> None:
    """`_timestamps` 唯一入口：DELIVER 同时写 delivered_at 与 auto_confirm_due_at。"""
    repo = MemoryOrderRepo()
    repo.seed(1, Status.IN_PROGRESS.value)
    result = apply(repo, ctx(last_node="ON_THE_WAY"), Action.DELIVER, RUNNER, now=NOW)
    patch = dict(result.status_patch)
    assert patch["delivered_at"] == NOW
    assert patch["auto_confirm_due_at"] == NOW + DEFAULT_THRESHOLDS.auto_confirm_delta
    # 一个事务里两个字段同源同刻
    assert patch["auto_confirm_due_at"] - patch["delivered_at"] == datetime.timedelta(hours=24)


def test_t14_rewrites_due_at_but_not_delivered_at() -> None:
    """BR-34 / K-12：T14 回 DELIVERED 时重写 auto_confirm_due_at，**绝不改 delivered_at**。"""
    delivered_long_ago = NOW - 72 * HOUR
    order = ctx(
        status=Status.DISPUTED.value,
        pre_dispute_status=Status.DELIVERED.value,
        delivered_at=delivered_long_ago,
        auto_confirm_due_at=NOW - 48 * HOUR,
    )
    repo = MemoryOrderRepo()
    repo.seed(1, Status.DISPUTED.value)
    result = apply(repo, order, Action.ARBITRATE_FREE, ADMIN, now=NOW)
    patch = dict(result.status_patch)
    assert result.to_status == Status.DELIVERED.value
    assert patch["auto_confirm_due_at"] == NOW + datetime.timedelta(hours=24)
    assert "delivered_at" not in patch  # 只读字段，永远不出现在 patch 里
    assert patch["pre_dispute_status"] is None


@pytest.mark.parametrize(
    "pre,expected_status",
    [
        # T13 回 IN_PROGRESS：这个状态根本不该有 24h 计时 → 必须清空
        (Status.IN_PROGRESS.value, Status.IN_PROGRESS.value),
        # T14 回 COMPLETED：已完成没有"待确认到期"这回事 → 也必须清空
        (Status.COMPLETED.value, Status.COMPLETED.value),
    ],
)
def test_leaving_disputed_clears_stale_due_at(pre: str, expected_status: str) -> None:
    """F2：出 DISPUTED **默认清空** `auto_confirm_due_at`（7.4 restore_status 的默认 patch）。

    只"设"不"清"的危害：投诉前的旧 `auto_confirm_due_at` 会残留在一个不需要 24h 计时的
    状态上，一旦它已过期，任何"顺手扫一遍到期单"的实现都会把进行中的单判成已完成。
    """
    order = ctx(
        status=Status.DISPUTED.value,
        pre_dispute_status=pre,
        delivered_at=NOW - 72 * HOUR,
        completed_at=NOW - 48 * HOUR,
        auto_confirm_due_at=NOW - 50 * HOUR,  # 陈旧值，必须被清掉
    )
    repo = MemoryOrderRepo()
    repo.seed(1, Status.DISPUTED.value)
    result = apply(repo, order, Action.ARBITRATE_FREE, ADMIN, now=NOW)
    patch = dict(result.status_patch)
    assert result.to_status == expected_status
    assert patch["pre_dispute_status"] is None
    assert patch.get("auto_confirm_due_at") is None, (
        f"出 DISPUTED（PRE={pre}）必须清空 auto_confirm_due_at，实得 "
        f"{patch.get('auto_confirm_due_at')}"
    )
    # 幂等性：patch 里出现该键且值为 None，才不会"什么都不写"导致旧值留下
    assert "auto_confirm_due_at" in patch


def test_force_close_from_disputed_also_clears_hangover() -> None:
    """F2 补充：管理员从 DISPUTED 强制关闭也是"出 DISPUTED"，同样要清干净。

    5.2 写明 `pre_dispute_status` 只在 `status='DISPUTED'` 时有意义；
    漏掉这条路径会留下 `status=CANCELLED` 却仍带挂起态的脏单。
    """
    order = ctx(
        status=Status.DISPUTED.value,
        pre_dispute_status=Status.DELIVERED.value,
        delivered_at=NOW - 72 * HOUR,
        auto_confirm_due_at=NOW - 48 * HOUR,
    )
    repo = MemoryOrderRepo()
    repo.seed(1, Status.DISPUTED.value)
    result = apply(
        repo, order, Action.FORCE_CLOSE, ADMIN,
        TransitionPayload(reason="线下已和解"), now=NOW,
    )
    patch = dict(result.status_patch)
    assert result.to_status == Status.CANCELLED.value
    assert patch["pre_dispute_status"] is None
    assert patch["auto_confirm_due_at"] is None
    assert patch["cancel_reason"] == "线下已和解"
    assert "delivered_at" not in patch


def test_force_close_outside_disputed_leaves_no_hangover_keys() -> None:
    """非 DISPUTED 状态强制关闭不需要（也不该）碰这两个字段。"""
    order = ctx(status=Status.IN_PROGRESS.value, pre_dispute_status=None)
    repo = MemoryOrderRepo()
    repo.seed(1, Status.IN_PROGRESS.value)
    result = apply(repo, order, Action.FORCE_CLOSE, ADMIN, now=NOW)
    patch = dict(result.status_patch)
    assert "pre_dispute_status" not in patch
    assert "auto_confirm_due_at" not in patch


# ---------------------------------------------------------------------------
# URGE（催单，FR-FULFILL-05）：补入 action 全集后的自环动作
# ---------------------------------------------------------------------------
def test_urge_positive_and_persisted_as_fact() -> None:
    """发单人第 1/2 次催单通过，且每次都落一条 `URGE` 事实（可审计、可举证）。"""
    repo = MemoryOrderRepo()
    repo.seed(1, Status.IN_PROGRESS.value)
    first = apply(repo, ctx(urge_count=0), Action.URGE, PUBLISHER, now=NOW)
    assert first.transition_id == "SELF-IN_PROGRESS-URGE"
    assert first.to_status == Status.IN_PROGRESS.value
    assert dict(first.status_patch) == {}          # 自环：不改任何字段、不改节点
    second = apply(repo, ctx(urge_count=1), Action.URGE, PUBLISHER, now=NOW)
    assert second.seq == 2
    assert repo.actions == ["URGE", "URGE"]        # 两次都留痕


def test_urge_third_time_rejected() -> None:
    """第 3 次催单（urge_count 已达上限 2）→ 42201。"""
    repo = MemoryOrderRepo()
    repo.seed(1, Status.IN_PROGRESS.value)
    with pytest.raises(ApiError) as exc_info:
        apply(repo, ctx(urge_count=2), Action.URGE, PUBLISHER, now=NOW)
    assert exc_info.value.code == 42201
    assert "最多 2 次" in exc_info.value.message
    assert repo.events == []


def test_urge_by_runner_is_forbidden() -> None:
    """接单人不能催自己的单 → 40310（越权，不是 42201）。"""
    repo = MemoryOrderRepo()
    repo.seed(1, Status.IN_PROGRESS.value)
    with pytest.raises(ApiError) as exc_info:
        apply(repo, ctx(urge_count=0), Action.URGE, RUNNER, now=NOW)
    assert (exc_info.value.http_status, exc_info.value.code) == (403, 40310)
    assert repo.events == []


def test_urge_is_not_runner_evidence() -> None:
    """`URGE` 不算接单人进度 —— 否则发单人一催单就关死自己的 T17 出口（7.4 反例）。"""
    assert Action.URGE.value not in RUNNER_EVIDENCE
    facts = [
        ProgressFact(action=Action.ACCEPT.value, actor_id=200, created_at=NOW - 60 * MIN),
        ProgressFact(action=Action.URGE.value, actor_id=100, created_at=NOW - 5 * MIN),
    ]
    assert probe_progress(facts, runner_id=200, accepted_at=NOW - 60 * MIN) == (
        False,
        NOW - 60 * MIN,
    )


def test_urge_only_available_in_progress() -> None:
    """`URGE` 只在 `IN_PROGRESS` 有出边（5.4 的状态表口径）。"""
    for status in Status:
        if status is Status.IN_PROGRESS:
            continue
        assert sm.transition_candidates(status, Action.URGE) == (), status


# ---------------------------------------------------------------------------
# 3. 非法迁移矩阵：7 状态 × 18 action 全笛卡尔积
# ---------------------------------------------------------------------------
_ALL_COMBOS = [
    (status, action)
    for status in Status
    for action in Action
    if (status.value, action) not in sm.LEGAL_TRANSITION_KEYS
]


@pytest.mark.parametrize(
    "status,action", _ALL_COMBOS, ids=[f"{s.value}--{a.value}" for s, a in _ALL_COMBOS]
)
def test_illegal_transition_matrix(status: Status, action: Action) -> None:
    """未在迁移表中的 `(状态, 动作)` 一律 42201（D3 可识别错误），不是 500、不是 403。"""
    order = ctx(status=status.value)
    repo = MemoryOrderRepo()
    repo.seed(1, status.value)
    with pytest.raises(ApiError) as exc_info:
        apply(repo, order, action, ADMIN, now=NOW)
    assert exc_info.value.code == ErrCode.ILLEGAL_TRANSITION == 42201
    assert exc_info.value.http_status == 422
    # 非法迁移不能留下任何事件或状态变更
    assert repo.events == []
    assert repo.rows[1]["status"] == status.value


def test_legal_matrix_size() -> None:
    """合法键 28 个：17 条 T 的 action 键（T5 两个、T13/T14 共用 1 个）+ 11 个辅助键。

    辅助 11 个 = REQUEST_CANCEL / REJECT_CANCEL / **URGE** 三个自环
    + 3 个冻结补证 + 5 个强制关闭。
    """
    assert len(sm.LEGAL_TRANSITION_KEYS) == 28
    assert len(_ALL_COMBOS) == 7 * 19 - 27  # 133 - 合法且 from_status 非 None 的 27 个


def test_unknown_action_returns_42201() -> None:
    """脏 action（不在 18 个枚举里）也必须是 42201，而不是 KeyError/500。"""
    repo = MemoryOrderRepo()
    repo.seed(1, Status.PENDING.value)
    with pytest.raises(ApiError) as exc_info:
        apply(repo, ctx(status=Status.PENDING.value), "TELEPORT", RUNNER, now=NOW)
    assert exc_info.value.code == 42201


# ---------------------------------------------------------------------------
# 4. T16 / T17 互斥（4 例，T-12 完成判据原文）
# ---------------------------------------------------------------------------
def _t17(order: OrderCtx) -> Any:
    repo = MemoryOrderRepo()
    repo.seed(order.id, order.status)
    return apply(repo, order, Action.CANCEL_LOST, PUBLISHER, now=NOW)


def _t16(order: OrderCtx) -> Any:
    repo = MemoryOrderRepo()
    repo.seed(order.id, order.status)
    return apply(repo, order, Action.DISPUTE_OPENED, PUBLISHER, now=NOW)


def _no_progress_ctx(elapsed: datetime.timedelta) -> OrderCtx:
    return ctx(
        has_runner_evidence=False,
        last_runner_evidence_at=None,
        accepted_at=NOW - elapsed,
    )


def _with_progress_ctx(elapsed: datetime.timedelta) -> OrderCtx:
    return ctx(
        has_runner_evidence=True,
        last_runner_evidence_at=NOW - elapsed,
        accepted_at=NOW - 5 * HOUR,
    )


def test_t16_t17_mutual_exclusion_1_no_progress_44m59s() -> None:
    """① 无进度 44:59 → T17 拒（未满 45 分钟）。"""
    order = _no_progress_ctx(45 * MIN - datetime.timedelta(seconds=1))
    with pytest.raises(ApiError) as exc_info:
        _t17(order)
    assert exc_info.value.code == 42201


def test_t16_t17_mutual_exclusion_2_no_progress_45m00s() -> None:
    """② 无进度 45:00 → T17 通过（边界含等号）。"""
    order = _no_progress_ctx(45 * MIN)
    result = _t17(order)
    assert result.transition_id == "T17"
    assert result.to_status == Status.CANCELLED.value
    # 副作用：信誉 −3（effects 里声明，实际派发在 T-28）
    assert "credit_minus_3" in result.effects


def test_t16_t17_mutual_exclusion_3_with_progress_45m00s() -> None:
    """③ 有备注进度 + 45:00 → T17 拒、T16 通过（这是两条路的分水岭）。"""
    order = _with_progress_ctx(45 * MIN)
    with pytest.raises(ApiError) as t17_exc:
        _t17(order)
    assert t17_exc.value.code == 42201
    assert "已产生进度" in t17_exc.value.message

    result = _t16(order)
    assert result.transition_id == "T16"
    assert result.to_status == Status.DISPUTED.value


def test_t16_t17_mutual_exclusion_4_with_progress_44m59s() -> None:
    """④ 有进度但 44:59 → T16 拒（静默未满），T17 也拒（有进度）。"""
    order = _with_progress_ctx(45 * MIN - datetime.timedelta(seconds=1))
    with pytest.raises(ApiError) as t16_exc:
        _t16(order)
    assert t16_exc.value.code == 42201
    assert "静默未满" in t16_exc.value.message

    with pytest.raises(ApiError):
        _t17(order)


# ---------------------------------------------------------------------------
# 5. 阈值边界（注入 now，不 sleep）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "elapsed,should_pass",
    [
        (datetime.timedelta(minutes=44, seconds=59), False),
        (datetime.timedelta(minutes=45, seconds=0), True),
        (datetime.timedelta(minutes=45, seconds=1), True),
        (datetime.timedelta(minutes=90), True),
    ],
)
def test_silent_threshold_boundary(
    elapsed: datetime.timedelta, should_pass: bool
) -> None:
    """44 分 59 秒拒、45 分 00 秒过 —— 用注入的 `now` 控制，不 sleep。"""
    order = _no_progress_ctx(elapsed)
    assert sm.guard_no_progress_45m(order, PUBLISHER, NOW) is should_pass

    order16 = _with_progress_ctx(elapsed)
    assert sm.guard_silent_45m(order16, PUBLISHER, NOW) is should_pass


def test_threshold_is_configurable() -> None:
    """阈值来自配置（K-11），不是硬编码：改成 30 分钟后 31 分钟就该放行。"""
    cfg = Thresholds(silent_minutes=30)
    order = _no_progress_ctx(31 * MIN)
    assert sm.guard_no_progress_45m(order, PUBLISHER, NOW) is False
    assert sm.guard_no_progress_45m(order, PUBLISHER, NOW, cfg) is True


# ---------------------------------------------------------------------------
# 6. 「进度记录」口径反例（7.4 三个反例，防回归）
# ---------------------------------------------------------------------------
def _facts(*items: tuple[str, int | None, datetime.timedelta]) -> list[ProgressFact]:
    return [
        ProgressFact(action=action, actor_id=actor_id, created_at=NOW - ago)
        for action, actor_id, ago in items
    ]


def test_publisher_note_is_not_progress() -> None:
    """反例①：发单人 `NOTE`（催单）不算接单人进度 → `has_progress=False`，T17 仍可走。"""
    facts = _facts(("ACCEPT", 200, 60 * MIN), ("NOTE", 100, 10 * MIN))
    has_progress, silent_since = probe_progress(facts, runner_id=200, accepted_at=NOW - 60 * MIN)
    assert has_progress is False
    assert silent_since == NOW - 60 * MIN  # 静默起点 = 抢单时刻，没被催单重置

    order = ctx(has_runner_evidence=False, last_runner_evidence_at=None, accepted_at=NOW - 60 * MIN)
    assert sm.guard_no_progress_45m(order, PUBLISHER, NOW) is True


def test_publisher_urge_and_cancel_request_are_not_progress() -> None:
    """反例②：`URGE` / `REQUEST_CANCEL` 同样不算进度（不在 RUNNER_EVIDENCE 白名单）。"""
    facts = _facts(
        ("ACCEPT", 200, 60 * MIN),
        ("URGE", 100, 20 * MIN),
        ("REQUEST_CANCEL", 100, 5 * MIN),
    )
    has_progress, silent_since = probe_progress(facts, runner_id=200, accepted_at=NOW - 60 * MIN)
    assert has_progress is False
    assert silent_since == NOW - 60 * MIN


def test_runner_note_makes_progress_and_resets_silent() -> None:
    """反例③：接单人 `NOTE` → `has_progress=True` 且静默起点被重置到该条备注时刻。"""
    facts = _facts(
        ("ACCEPT", 200, 120 * MIN),
        ("PICKED_UP", 200, 100 * MIN),
        ("NOTE", 200, 10 * MIN),
    )
    has_progress, silent_since = probe_progress(
        facts, runner_id=200, accepted_at=NOW - 120 * MIN
    )
    assert has_progress is True
    assert silent_since == NOW - 10 * MIN

    # 静默只过了 10 分钟 → T16 拒（有进度但没静默够）、T17 也拒（有进度）
    order = ctx(
        has_runner_evidence=True,
        last_runner_evidence_at=NOW - 10 * MIN,
        accepted_at=NOW - 120 * MIN,
    )
    assert sm.guard_silent_45m(order, PUBLISHER, NOW) is False
    assert sm.guard_no_progress_45m(order, PUBLISHER, NOW) is False


def test_progress_without_runner_id_is_ignored() -> None:
    """`runner_id` 为 None（未接单）时任何事件都不算进度。"""
    facts = _facts(("PICKED_UP", 200, 10 * MIN))
    assert probe_progress(facts, runner_id=None, accepted_at=None) == (False, None)


# ---------------------------------------------------------------------------
# 7. 越权：接单人自调发单人动作 → 40310
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,action",
    [
        # T17 的操作者只有发单人
        (Status.IN_PROGRESS, Action.CANCEL_LOST),
        # T16 的操作者是「发单人」而不是「任一方」—— 接单人不能靠它冻结订单
        (Status.IN_PROGRESS, Action.DISPUTE_OPENED),
        # T9 只有发单人能确认收货
        (Status.DELIVERED, Action.CONFIRM),
    ],
    ids=["T17", "T16", "T9"],
)
def test_runner_cannot_call_publisher_actions(status: Status, action: Action) -> None:
    """接单人调发单人的出口必须 40310（不是 42201，也不是 200）。"""
    order = ctx(status=status.value, has_runner_evidence=False, last_runner_evidence_at=None,
                accepted_at=NOW - 60 * MIN, delivered_at=NOW - HOUR)
    repo = MemoryOrderRepo()
    repo.seed(1, status.value)
    with pytest.raises(ApiError) as exc_info:
        apply(repo, order, action, RUNNER, now=NOW)
    assert exc_info.value.http_status == 403
    assert exc_info.value.code == ErrCode.ORDER_NOT_YOURS == 40310
    assert repo.events == []


def test_t16_only_publisher_even_for_other_party() -> None:
    """T16 的操作者是「发单人」而非「任一方」：接单人不能拿它冻结订单。"""
    order = _with_progress_ctx(60 * MIN)
    assert sm.guard_silent_45m(order, RUNNER, NOW) is False
    assert sm.guard_silent_45m(order, PUBLISHER, NOW) is True


# ---------------------------------------------------------------------------
# 8. 节点顺序（T5 顺序推进 / T7 已到最后节点）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "last_node,action,expected",
    [
        (None, Action.PICKED_UP, True),           # 第一步：已取件
        ("PICKED_UP", Action.ON_THE_WAY, True),   # 第二步：配送中
        (None, Action.ON_THE_WAY, False),         # **跳级**：不许跳过「已取件」
        (None, Action.DELIVER, False),            # **跳级**：一步到位完成整单
        ("PICKED_UP", Action.PICKED_UP, False),   # 重复节点：不推进
        ("ON_THE_WAY", Action.PICKED_UP, False),  # 回退：禁止
        ("ON_THE_WAY", Action.ON_THE_WAY, False),
        ("垃圾节点", Action.DELIVER, False),       # 脏数据不猜，直接拒
    ],
)
def test_node_must_advance_in_order(last_node: str | None, action: Action, expected: bool) -> None:
    """T5 严格顺序推进：只能推进到**下一个**节点，跳级一律拒（证据链不能缺环）。"""
    assert sm._node_in_order(ctx(last_node=last_node), RUNNER, NOW, action=action) is expected


def test_skip_picked_up_is_rejected() -> None:
    """跳级 `None → ON_THE_WAY` 必须 42201：否则时间轴上永远没有 PICKED_UP。"""
    repo = MemoryOrderRepo()
    repo.seed(1, Status.IN_PROGRESS.value)
    with pytest.raises(ApiError) as exc_info:
        apply(repo, ctx(last_node=None), Action.ON_THE_WAY, RUNNER, now=NOW)
    assert exc_info.value.code == 42201
    assert repo.events == []
    # 顺序推进则通过
    result = apply(repo, ctx(last_node=None), Action.PICKED_UP, RUNNER, now=NOW)
    assert result.transition_id == "T5"


def test_deliver_requires_last_node() -> None:
    """跳级 PICKED_UP→DELIVER 必须被拒（T-24 的验收点）。"""
    order = ctx(last_node="PICKED_UP")
    repo = MemoryOrderRepo()
    repo.seed(1, Status.IN_PROGRESS.value)
    with pytest.raises(ApiError) as exc_info:
        apply(repo, order, Action.DELIVER, RUNNER, now=NOW)
    assert exc_info.value.code == 42201
    # 到齐之后可以
    ok = apply(repo, ctx(last_node="ON_THE_WAY"), Action.DELIVER, RUNNER, now=NOW)
    assert ok.to_status == Status.DELIVERED.value


# ---------------------------------------------------------------------------
# 9. CAS 并发冲突 → 40905
# ---------------------------------------------------------------------------
def test_cas_conflict_returns_40905() -> None:
    """CAS 0 行说明状态被别人改了：必须 40905（可刷新重试），不是 42201。"""
    repo = MemoryOrderRepo()
    repo.seed(1, Status.IN_PROGRESS.value)
    repo.fail_next_cas = True
    with pytest.raises(ApiError) as exc_info:
        apply(repo, ctx(last_node=None), Action.PICKED_UP, RUNNER, now=NOW)
    assert exc_info.value.http_status == 409
    assert exc_info.value.code == ErrCode.STATUS_CHANGED_CONCURRENTLY == 40905
    # 事实已经 append（先事实后聚合），由外层事务回滚 —— K-09
    assert len(repo.events) == 1


def test_stale_order_status_is_illegal_not_conflict() -> None:
    """用过期快照调 apply：查表阶段就该 42201（CAS 根本走不到）。"""
    repo = MemoryOrderRepo()
    repo.seed(1, Status.CANCELLED.value)
    with pytest.raises(ApiError) as exc_info:
        apply(repo, ctx(status=Status.CANCELLED.value, last_node=None), Action.PICKED_UP,
              RUNNER, now=NOW)
    assert exc_info.value.code == 42201


# ---------------------------------------------------------------------------
# 10. 网格：T13/T14 的目标态由 pre_dispute_status 决定
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pre,expected",
    [
        (Status.IN_PROGRESS.value, Status.IN_PROGRESS.value),
        (Status.DELIVERED.value, Status.DELIVERED.value),
        (Status.COMPLETED.value, Status.COMPLETED.value),
    ],
)
def test_arbitrate_free_restores_pre_dispute_status(pre: str, expected: str) -> None:
    """T13/T14 共用 `(DISPUTED, ARBITRATE_FREE)` 键，靠守卫选，目标态 = 投诉前状态。"""
    order = ctx(status=Status.DISPUTED.value, pre_dispute_status=pre,
                completed_at=NOW - HOUR, delivered_at=NOW - 2 * HOUR)
    repo = MemoryOrderRepo()
    repo.seed(1, Status.DISPUTED.value)
    result = apply(repo, order, Action.ARBITRATE_FREE, ADMIN, now=NOW)
    assert result.to_status == expected
    assert result.transition_id == ("T13" if pre == Status.IN_PROGRESS.value else "T14")


def test_arbitrate_free_without_pre_status_is_42202() -> None:
    """挂起态缺失（脏数据）→ 42202，明确要求人工核对，而不是被 T13 的 hint 引向 T14。"""
    order = ctx(status=Status.DISPUTED.value, pre_dispute_status=None)
    repo = MemoryOrderRepo()
    repo.seed(1, Status.DISPUTED.value)
    with pytest.raises(ApiError) as exc_info:
        apply(repo, order, Action.ARBITRATE_FREE, ADMIN, now=NOW)
    assert exc_info.value.code == ErrCode.PRE_DISPUTE_STATUS_MISSING == 42202
    # 必须是 42202 而不是 42201：42201 会带出「应走 T14」的误导性提示
    assert exc_info.value.code != ErrCode.ILLEGAL_TRANSITION
    assert "人工核对" in exc_info.value.message
    assert repo.events == []


@pytest.mark.parametrize(
    "actor",
    [RUNNER, PUBLISHER, SYSTEM],
    ids=["RUNNER", "PUBLISHER", "SYSTEM"],
)
def test_arbitrate_free_without_pre_status_by_non_admin_is_40310(actor: Actor) -> None:
    """P3：越权者必须先拿 40310，**不能**拿到 42202。

    `42202`（挂起态缺失）是"数据有异常，需人工核对"的内部状态，只有有权执行该动作的
    角色才该看到。若预检查抢在角色校验之前：
    ① 把"无权操作"误报成"数据异常"，管理员排障方向被带偏；
    ② 越权调用方（接单人/发单人/系统）能从错误码反推出「该单挂起态缺失」——
       这是一条不该存在的泄露通道。
    """
    order = ctx(status=Status.DISPUTED.value, pre_dispute_status=None)
    repo = MemoryOrderRepo()
    repo.seed(1, Status.DISPUTED.value)
    with pytest.raises(ApiError) as exc_info:
        apply(repo, order, Action.ARBITRATE_FREE, actor, now=NOW)
    assert (exc_info.value.http_status, exc_info.value.code) == (403, 40310)
    assert exc_info.value.code != ErrCode.PRE_DISPUTE_STATUS_MISSING
    assert repo.events == []
    assert repo.rows[1]["status"] == Status.DISPUTED.value


def test_arbitrate_liable_clears_pre_status() -> None:
    order = ctx(status=Status.DISPUTED.value, pre_dispute_status=Status.DELIVERED.value)
    repo = MemoryOrderRepo()
    repo.seed(1, Status.DISPUTED.value)
    result = apply(repo, order, Action.ARBITRATE_LIABLE, ADMIN,
                   TransitionPayload(reason="接单人担责"), now=NOW)
    assert result.to_status == Status.CANCELLED.value
    patch = dict(result.status_patch)
    assert patch["pre_dispute_status"] is None
    assert patch["cancel_reason"] == "接单人担责"


def test_double_dispute_blocked_by_guard() -> None:
    """BR-28 同单仅 1 次纠纷：`dispute_count=1` 时 T11/T12 守卫必须拒（而不是撞唯一约束 500）。"""
    order = ctx(status=Status.DELIVERED.value, delivered_at=NOW - HOUR, dispute_count=1)
    repo = MemoryOrderRepo()
    repo.seed(1, Status.DELIVERED.value)
    with pytest.raises(ApiError) as exc_info:
        apply(repo, order, Action.DISPUTE_OPENED, PUBLISHER, now=NOW)
    assert exc_info.value.code == 42201


def test_t12_window_closed() -> None:
    """完成后 >48h 投诉 → 42201（T12 时间窗）。"""
    order = ctx(status=Status.COMPLETED.value, completed_at=NOW - 49 * HOUR)
    repo = MemoryOrderRepo()
    repo.seed(1, Status.COMPLETED.value)
    with pytest.raises(ApiError) as exc_info:
        apply(repo, order, Action.DISPUTE_OPENED, PUBLISHER, now=NOW)
    assert exc_info.value.code == 42201


def test_auto_confirm_uses_due_at_not_delivered_at() -> None:
    """T10 到期判定只看 `auto_confirm_due_at`：delivered_at 是 3 天前也不算到期。"""
    order = ctx(
        status=Status.DELIVERED.value,
        delivered_at=NOW - 72 * HOUR,        # 3 天前送达（回滚单的典型形态）
        auto_confirm_due_at=NOW + 23 * HOUR,  # 但到期时刻被重写为明天
    )
    assert sm.guard_auto_confirm_due(order, SYSTEM, NOW) is False
    order_due = ctx(
        status=Status.DELIVERED.value,
        delivered_at=NOW - 72 * HOUR,
        auto_confirm_due_at=NOW,
    )
    assert sm.guard_auto_confirm_due(order_due, SYSTEM, NOW) is True


# ---------------------------------------------------------------------------
# 11. 投影一致性：随机 1000 条事件序列
# ---------------------------------------------------------------------------
@dataclass
class _Walk:
    """随机游走的状态容器：按真实迁移推进，记录事件序列。"""

    repo: MemoryOrderRepo
    order_id: int = 1
    status: str = Status.PENDING.value
    last_node: str | None = None
    has_evidence: bool = False
    last_evidence_at: datetime.datetime | None = None
    accepted_at: datetime.datetime | None = None
    delivered_at: datetime.datetime | None = None
    completed_at: datetime.datetime | None = None
    due_at: datetime.datetime | None = None
    dispute_count: int = 0
    pre_dispute_status: str | None = None
    cancel_request_status: str | None = None
    events: list[tuple[Action, str]] = field(default_factory=list)

    def ctx(self) -> OrderCtx:
        return ctx(
            id=self.order_id,
            status=self.status,
            # 未接单前没有接单人：否则 T2 的守卫（runner_id 必须为 NULL）永远不过
            runner_id=None if self.status == Status.PENDING.value else 200,
            has_runner_evidence=self.has_evidence,
            last_runner_evidence_at=self.last_evidence_at,
            accepted_at=self.accepted_at,
            delivered_at=self.delivered_at,
            completed_at=self.completed_at,
            auto_confirm_due_at=self.due_at,
            last_node=self.last_node,
            dispute_count=self.dispute_count,
            pre_dispute_status=self.pre_dispute_status,
            cancel_request_status=self.cancel_request_status,
            deadline=NOW + 2 * HOUR,
            runner_inflight_count=0,
            publisher_pending_count=0,
        )

    def absorb(self, result: Any, action: Action) -> None:
        self.status = result.to_status
        patch = dict(result.status_patch)
        self.events.append((action, result.to_status))
        if "accepted_at" in patch:
            self.accepted_at = patch["accepted_at"]
        if "delivered_at" in patch:
            self.delivered_at = patch["delivered_at"]
        if "completed_at" in patch:
            self.completed_at = patch["completed_at"]
        if "auto_confirm_due_at" in patch:
            self.due_at = patch["auto_confirm_due_at"]
        if "pre_dispute_status" in patch:
            self.pre_dispute_status = patch["pre_dispute_status"]
        if action is Action.DISPUTE_OPENED:
            self.dispute_count += 1
        if action in (Action.PICKED_UP, Action.ON_THE_WAY):
            self.last_node = action.value
        elif action is Action.DELIVER:
            self.last_node = Action.DELIVER.value
        if action in (Action.PICKED_UP, Action.ON_THE_WAY, Action.DELIVER, Action.NOTE):
            self.has_evidence = True
            self.last_evidence_at = NOW
        if action is Action.APPROVE_CANCEL:
            self.cancel_request_status = None
        if action is Action.ACCEPT:
            self.cancel_request_status = None


def _random_walk(rng: random.Random, steps: int) -> _Walk:
    walk = _Walk(repo=MemoryOrderRepo())
    walk.repo.seed(walk.order_id, walk.status)
    # T2 之前必须先有 ACCEPT 才能有 runner_id：OrderCtx 里 runner_id 恒为 200，
    # PENDING 阶段守卫不看它，所以直接开始走即可。
    actors = {"publisher": PUBLISHER, "runner": RUNNER, "admin": ADMIN, "system": SYSTEM}
    for _ in range(steps):
        candidates = [
            (status_key, action)
            for (status_key, action) in sm.LEGAL_TRANSITION_KEYS
            if status_key == walk.status
        ]
        if not candidates:
            break
        _, action = rng.choice(candidates)
        for role_name in rng.sample(list(actors), len(actors)):
            actor = actors[role_name]
            try:
                result = apply(walk.repo, walk.ctx(), action, actor, now=NOW)
            except ApiError:
                continue
            walk.absorb(result, action)
            break
    return walk


def test_projection_matches_status() -> None:
    """投影规则：`status == last(STATUS_MUTATING 事件).to_status`（5.4 对账口径）。

    随机生成 1000 条事件序列（每条 1..12 步随机游走），逐条比对聚合与投影。
    """
    rng = random.Random(20250601)
    checked = 0
    for _ in range(1000):
        walk = _random_walk(rng, rng.randint(1, 12))
        if not walk.events:
            continue
        projected = project_status(walk.events)
        assert projected == walk.status, (
            f"投影不一致：聚合={walk.status} 投影={projected} 事件={walk.events}"
        )
        # 事件流里每一条都必须属于 18 个 action 之一
        assert all(action in sm.PROGRESS_ACTIONS for action, _ in walk.events)
        checked += 1
    assert checked >= 900  # 允许少量序列一步都走不动（守卫全拒），但不能是 0


def test_projection_ignores_self_loops() -> None:
    """自环事件不影响投影结果。"""
    events = [
        (Action.ACCEPT, "IN_PROGRESS"),
        (Action.PICKED_UP, "IN_PROGRESS"),
        (Action.NOTE, "IN_PROGRESS"),
        (Action.REQUEST_CANCEL, "IN_PROGRESS"),
        (Action.REJECT_CANCEL, "IN_PROGRESS"),
    ]
    assert project_status(events) == "IN_PROGRESS"
    assert project_status([]) is None


# ---------------------------------------------------------------------------
# 12. available_actions（FR-CENTER-05 的按钮渲染依据）
# ---------------------------------------------------------------------------
def test_available_actions_by_role() -> None:
    in_progress = ctx(status=Status.IN_PROGRESS.value)
    pub_actions = set(sm.available_actions(in_progress, PUBLISHER))
    run_actions = set(sm.available_actions(in_progress, RUNNER))
    # 发单人：有进度 → 只有投诉（T16），没有 T17 无责取消这条通道；另有催单自环
    assert Action.DISPUTE_OPENED in pub_actions
    assert Action.CANCEL_LOST in pub_actions  # 角色层放行，守卫层再拒（_has_progress 时）
    assert Action.URGE in pub_actions
    # 接单人：可推进节点、留备注、送达、协商取消
    assert {Action.PICKED_UP, Action.ON_THE_WAY, Action.NOTE, Action.DELIVER} <= run_actions
    assert Action.CANCEL_LOST not in run_actions
    assert Action.URGE not in run_actions
    # 管理员在 IN_PROGRESS 下只有「强制关闭」这一个兜底动作（BR-32 / T-30），
    # 仲裁类动作只在 DISPUTED 出现
    assert sm.available_actions(in_progress, ADMIN) == (Action.FORCE_CLOSE,)


def test_terminal_states_have_no_actions() -> None:
    for status in (Status.CANCELLED, Status.EXPIRED):
        order = ctx(status=status.value)
        assert sm.available_actions(order, PUBLISHER) == ()
        assert sm.available_actions(order, RUNNER) == ()


def test_illegal_action_helper_shape() -> None:
    err = illegal_action(Status.PENDING, Action.CONFIRM)
    assert (err.http_status, err.code) == (422, 42201)
    assert "PENDING" in err.message and "CONFIRM" in err.message


def test_pre_dispute_sentinel_value() -> None:
    """T14 的目标态是哨兵而不是写死 COMPLETED（PRD 表格与 BR-34 的张力，见 5.4 出口裁定）。"""
    t14 = next(t for t in TRANSITIONS if t.id == "T14")
    assert t14.to_status == PRE_DISPUTE
    assert t14.to_status != Status.COMPLETED


def test_role_enum_matches_db() -> None:
    from app.models.order import ACTOR_ROLES

    assert {r.value for r in Role} == set(ACTOR_ROLES)


def test_utc_now_is_used_everywhere() -> None:
    """`now=None` 时用进程时钟，且必须是 UTC aware（禁止 naive）。"""
    repo = MemoryOrderRepo()
    repo.seed(1, Status.IN_PROGRESS.value)
    result = apply(repo, ctx(last_node=None), Action.PICKED_UP, RUNNER)
    _, created_at = repo.events[0]
    assert created_at.tzinfo is not None
    assert created_at.utcoffset() == datetime.timedelta(0)
    assert result.seq == 1
    assert UTC is not None
