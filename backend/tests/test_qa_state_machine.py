"""QA 独立验证（Edward）：T-12 状态机内核。

**刻意不复用 `tests/test_state_machine.py` 的任何断言或夹具**，
只共用 `conftest.MemoryOrderRepo` 这一个内存替身（它是 repo 协议的实现，
换一个就等于测另一个东西）。所有期望值直接从
`docs/01-PRD.md` 第 363–383 行的迁移清单人工抄录，不引用实现里的任何常量。

四组：
- C7：17 条迁移五要素（from / action / to / 操作者 / 守卫）逐条对账 + 守卫反例
- C8：任务书点名的 4 类边界场景，独立重写
- C9：7 状态 × 19 action × 4 角色全笛卡尔积，断言拒绝时零副作用
- C10：抗"假通过"——patch 的键必须是真实 DB 列（内存 repo 不会替我们校验）
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any

import pytest
from app.core.clock import UTC
from app.core.errors import ApiError, ErrCode
from app.modules.order import state_machine as sm
from app.modules.order.state_machine import (
    Action,
    Actor,
    OrderCtx,
    Role,
    Status,
    TransitionPayload,
)

from tests.conftest import MemoryOrderRepo

NOW = datetime.datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
SEC = datetime.timedelta(seconds=1)
MIN = datetime.timedelta(minutes=1)
HOUR = datetime.timedelta(hours=1)
DAY = datetime.timedelta(days=1)

PUBLISHER = Actor.publisher(100)
RUNNER = Actor.runner(200)
ADMIN = Actor.admin(900)
SYSTEM = Actor.system()


def order(**over: Any) -> OrderCtx:
    """一个"各项条件都满足"的 IN_PROGRESS 订单；用例只覆盖关心的字段。"""
    base: dict[str, Any] = {
        "id": 1,
        "status": Status.IN_PROGRESS.value,
        "publisher_id": 100,
        "runner_id": 200,
        "accepted_at": NOW - 2 * HOUR,
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
    base.update(over)
    return OrderCtx(**base)


def run(ctx: OrderCtx, action: Action, actor: Actor, **kw: Any) -> Any:
    """跑一次 apply，返回 (result, repo)；异常直接抛给调用方断言。"""
    repo = MemoryOrderRepo()
    repo.seed(ctx.id, ctx.status)
    payload = kw.pop("payload", None)
    return sm.apply(repo, ctx, action, actor, payload, now=NOW, **kw), repo


def expect_error(ctx: OrderCtx, action: Action, actor: Actor, code: int) -> ApiError:
    """调一次 apply，断言抛指定 code，并断言**零副作用**（无事件、状态未变）。"""
    repo = MemoryOrderRepo()
    repo.seed(ctx.id, ctx.status)
    with pytest.raises(ApiError) as exc:
        sm.apply(repo, ctx, action, actor, now=NOW)
    err = exc.value
    assert err.code == code, f"期望 code={code} 实得 {err.code}（{err.message}）"
    assert repo.events == [], "被拒绝的迁移不得追加任何 order_progress 事实"
    assert repo.rows[ctx.id]["status"] == ctx.status, "被拒绝的迁移不得改变订单状态"
    return err


# ---------------------------------------------------------------------------
# C7. 17 条迁移五要素对账（期望值人工抄自 PRD 363–383 行）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    tid: str
    from_status: Status | None       # PRD 的「当前」
    actions: tuple[Action, ...]      # PRD 的「触发动作」
    to_status: Status | str          # PRD 的「目标」（T14 是「原状态」= 动态）
    actors: frozenset[Role]          # PRD 的「操作者」
    prd_guard: str                   # PRD 的「守卫条件」原文（人读）


PRD_TABLE: tuple[Spec, ...] = (
    Spec("T1", None, (Action.CREATE,), Status.PENDING,
         frozenset({Role.PUBLISHER}), "已认证；发单时间窗内；同时待接单 ≤ 3"),
    Spec("T2", Status.PENDING, (Action.ACCEPT,), Status.IN_PROGRESS,
         frozenset({Role.RUNNER}), "原子抢占；在途 ≤ 3；未过期"),
    Spec("T3", Status.PENDING, (Action.CANCEL,), Status.CANCELLED,
         frozenset({Role.PUBLISHER}), "—"),
    Spec("T4", Status.PENDING, (Action.EXPIRE,), Status.EXPIRED,
         frozenset({Role.SYSTEM}), "超过期望送达时间"),
    Spec("T5", Status.IN_PROGRESS, (Action.PICKED_UP, Action.ON_THE_WAY), Status.IN_PROGRESS,
         frozenset({Role.RUNNER}), "节点只能顺序推进；重置静默计时"),
    Spec("T6", Status.IN_PROGRESS, (Action.NOTE,), Status.IN_PROGRESS,
         frozenset({Role.RUNNER}), "重置静默计时；使 BR-33 通道永久关闭"),
    Spec("T7", Status.IN_PROGRESS, (Action.DELIVER,), Status.DELIVERED,
         frozenset({Role.RUNNER}), "已到最后节点"),
    Spec("T8", Status.IN_PROGRESS, (Action.APPROVE_CANCEL,), Status.CANCELLED,
         frozenset({Role.PUBLISHER, Role.RUNNER}), "对方同意"),
    Spec("T9", Status.DELIVERED, (Action.CONFIRM,), Status.COMPLETED,
         frozenset({Role.PUBLISHER}), "—"),
    Spec("T10", Status.DELIVERED, (Action.AUTO_CONFIRM,), Status.COMPLETED,
         frozenset({Role.SYSTEM}), "送达 ≥ 24h 且未操作"),
    Spec("T11", Status.DELIVERED, (Action.DISPUTE_OPENED,), Status.DISPUTED,
         frozenset({Role.PUBLISHER, Role.RUNNER}), "同单仅 1 次纠纷"),
    Spec("T12", Status.COMPLETED, (Action.DISPUTE_OPENED,), Status.DISPUTED,
         frozenset({Role.PUBLISHER, Role.RUNNER}), "完成后 ≤ 48h（+ 同单仅 1 次纠纷）"),
    Spec("T13", Status.DISPUTED, (Action.ARBITRATE_FREE,), Status.IN_PROGRESS,
         frozenset({Role.ADMIN}), "pre_dispute_status = 进行中（BR-34）"),
    Spec("T14", Status.DISPUTED, (Action.ARBITRATE_FREE,), sm.PRE_DISPUTE,
         frozenset({Role.ADMIN}),
         "pre_dispute_status ∈ {待确认, 已完成}；目标态 = 投诉前状态本身"),
    Spec("T15", Status.DISPUTED, (Action.ARBITRATE_LIABLE,), Status.CANCELLED,
         frozenset({Role.ADMIN}), "—"),
    Spec("T16", Status.IN_PROGRESS, (Action.DISPUTE_OPENED,), Status.DISPUTED,
         frozenset({Role.PUBLISHER}), "已有进度记录 且 静默 ≥ 45 分钟（BR-28）；免举证"),
    Spec("T17", Status.IN_PROGRESS, (Action.CANCEL_LOST,), Status.CANCELLED,
         frozenset({Role.PUBLISHER}), "无任何进度记录 且 距抢单 ≥ 45 分钟（BR-33）；信誉 −3"),
)


def test_c7_transition_count_and_ids() -> None:
    """PRD 清单是 17 行 → 实现必须恰好 17 条，且编号 T1..T17 不重不漏不乱序。"""
    assert len(PRD_TABLE) == 17
    got_ids = [t.id for t in sm.TRANSITIONS]
    assert got_ids == [f"T{i}" for i in range(1, 18)], got_ids


@pytest.mark.parametrize("spec", PRD_TABLE, ids=[s.tid for s in PRD_TABLE])
def test_c7_five_elements_match_prd(spec: Spec) -> None:
    """逐条比对 from / action / to / 操作者（四要素；守卫在下一个用例里逐条反例验）。"""
    trans = next(t for t in sm.TRANSITIONS if t.id == spec.tid)

    # ① from
    assert trans.from_status == spec.from_status, (
        f"{spec.tid} from 不一致：PRD={spec.from_status} 实现={trans.from_status}"
    )
    # ② action 集合
    assert set(trans.actions) == set(spec.actions), (
        f"{spec.tid} action 不一致：PRD={[a.value for a in spec.actions]} "
        f"实现={[a.value for a in trans.actions]}"
    )
    # ③ to（T14 是动态目标，用哨兵）
    assert trans.to_status == spec.to_status, (
        f"{spec.tid} to 不一致：PRD={spec.to_status} 实现={trans.to_status}"
    )
    # ④ 操作者
    assert trans.actors == spec.actors, (
        f"{spec.tid} 操作者不一致：PRD={sorted(r.value for r in spec.actors)} "
        f"实现={sorted(r.value for r in trans.actors)}"
    )
    # kind 必须是 T（编号即验收依据）
    assert trans.kind is sm.TransKind.T


def test_c7_every_transition_reachable_by_table_lookup() -> None:
    """17 条都必须能从 `(from, action)` 查表命中（否则就是死条目）。"""
    for spec in PRD_TABLE:
        for action in spec.actions:
            cands = sm.transition_candidates(spec.from_status, action)
            assert spec.tid in [c.id for c in cands], (
                f"{spec.tid} 无法通过 transition_candidates({spec.from_status}, {action}) 命中"
            )


# ---- ⑤ 守卫：每条迁移至少一个「角色合法但条件不满足 → 42201」的反例 ----------
# 结构：(tid, 说明, ctx 覆盖, actor, 期望 code, action 覆盖)
# 42201 = 守卫拒（条件不满足）；40310 = 角色不在 actors（越权）
# `action` 为 None 时取该迁移的动作；T5 有两个动作，必须逐条点名。
GUARD_NEGATIVES: tuple[tuple[str, str, dict[str, Any], Actor, int, Action | None], ...] = (
    # T1：已认证 / 时间窗 / 待接单 ≤3
    ("T1", "未认证不能发单", {"publisher_pending_count": 0},
     Actor.publisher(100, is_verified=False), ErrCode.ILLEGAL_TRANSITION, None),
    ("T1", "同时待接单已 3 单", {"publisher_pending_count": 3}, PUBLISHER,
     ErrCode.ILLEGAL_TRANSITION, None),
    # T2：在途 ≤3 / 未过期 / 不能接自己的单
    ("T2", "在途已满 3 单", {"runner_inflight_count": 3}, RUNNER,
     ErrCode.ILLEGAL_TRANSITION, None),
    ("T2", "订单已过期", {"deadline": NOW}, RUNNER, ErrCode.ILLEGAL_TRANSITION, None),
    ("T2", "不能接自己发布的单", {}, Actor.runner(100), ErrCode.ILLEGAL_TRANSITION, None),
    # T3/T9：仅发单人本人
    ("T3", "接单人不能取消待接单", {}, RUNNER, ErrCode.ORDER_NOT_YOURS, None),
    # T4：系统动作 + 必须真过期
    ("T4", "未到期的单不能被置过期", {"deadline": NOW + 1 * SEC}, SYSTEM,
     ErrCode.ILLEGAL_TRANSITION, None),
    ("T4", "非系统不能置过期", {"deadline": NOW - SEC}, PUBLISHER,
     ErrCode.ORDER_NOT_YOURS, None),
    # T5：节点只能顺序推进（回退与跳级都拒）
    ("T5", "已到 ON_THE_WAY 不能回退 PICKED_UP", {"last_node": "ON_THE_WAY"},
     RUNNER, ErrCode.ILLEGAL_TRANSITION, Action.PICKED_UP),
    ("T5", "重复点 ON_THE_WAY 被拒", {"last_node": "ON_THE_WAY"},
     RUNNER, ErrCode.ILLEGAL_TRANSITION, Action.ON_THE_WAY),
    # T6：仅接单人本人
    ("T6", "发单人不能留接单人备注", {}, PUBLISHER, ErrCode.ORDER_NOT_YOURS, None),
    # T7：必须已到最后节点（跳级拒绝）
    ("T7", "只到 PICKED_UP 不能标送达", {"last_node": "PICKED_UP"}, RUNNER,
     ErrCode.ILLEGAL_TRANSITION, None),
    ("T7", "一个节点都没点不能标送达", {"last_node": None}, RUNNER,
     ErrCode.ILLEGAL_TRANSITION, None),
    # T8：对方同意是硬前提
    ("T8", "对方未同意不能取消", {"cancel_request_status": "PENDING"}, PUBLISHER,
     ErrCode.ILLEGAL_TRANSITION, None),
    ("T8", "没有协商申请不能取消", {"cancel_request_status": None}, RUNNER,
     ErrCode.ILLEGAL_TRANSITION, None),
    # T9：仅发单人
    ("T9", "接单人不能确认收货", {"status": Status.DELIVERED.value}, RUNNER,
     ErrCode.ORDER_NOT_YOURS, None),
    # T10：只看 auto_confirm_due_at
    ("T10", "未到 due_at 不能自动确认",
     {"status": Status.DELIVERED.value, "auto_confirm_due_at": NOW + 1 * SEC}, SYSTEM,
     ErrCode.ILLEGAL_TRANSITION, None),
    ("T10", "due_at 缺失不能自动确认",
     {"status": Status.DELIVERED.value, "auto_confirm_due_at": None}, SYSTEM,
     ErrCode.ILLEGAL_TRANSITION, None),
    ("T10", "非系统不能自动确认",
     {"status": Status.DELIVERED.value, "auto_confirm_due_at": NOW - SEC}, PUBLISHER,
     ErrCode.ORDER_NOT_YOURS, None),
    # T11：同单仅一次
    ("T11", "已有纠纷不能再投诉",
     {"status": Status.DELIVERED.value, "dispute_count": 1}, PUBLISHER,
     ErrCode.ILLEGAL_TRANSITION, None),
    ("T11", "局外人不能投诉",
     {"status": Status.DELIVERED.value}, Actor.publisher(999),
     ErrCode.ILLEGAL_TRANSITION, None),
    # T12：48h 窗口 + 同单仅一次
    ("T12", "完成后 48h 已过",
     {"status": Status.COMPLETED.value, "completed_at": NOW - 48 * HOUR - SEC}, PUBLISHER,
     ErrCode.ILLEGAL_TRANSITION, None),
    ("T12", "已有纠纷不能再投诉",
     {"status": Status.COMPLETED.value, "completed_at": NOW - HOUR, "dispute_count": 1},
     PUBLISHER, ErrCode.ILLEGAL_TRANSITION, None),
    # T13/T14 共用 (DISPUTED, ARBITRATE_FREE)，所以"PRE 不对"不是错误而是走另一条；
    # 真正该被拒的是 PRE 两个守卫都不认（None / 脏值）。
    # **F4 裁定**：PRE 缺失时先抛 42202（挂起态缺失、需人工核对），
    # 而不是 42201（那会把管理员引向 T13 的 hint「应走 T14」，而 T14 同样走不通）。
    ("T13", "PRE 缺失时先抛 42202（挂起态缺失）",
     {"status": Status.DISPUTED.value, "pre_dispute_status": None}, ADMIN,
     ErrCode.PRE_DISPUTE_STATUS_MISSING, None),
    ("T14", "非管理员不能裁决",
     {"status": Status.DISPUTED.value, "pre_dispute_status": "DELIVERED"}, PUBLISHER,
     ErrCode.ORDER_NOT_YOURS, None),
    # T15：仅管理员
    ("T15", "非管理员不能裁决担责",
     {"status": Status.DISPUTED.value, "pre_dispute_status": "IN_PROGRESS"}, PUBLISHER,
     ErrCode.ORDER_NOT_YOURS, None),
    # T16：有进度 + 静默 ≥45min + 仅发单人
    ("T16", "无进度不能走 T16",
     {"has_runner_evidence": False, "last_runner_evidence_at": None}, PUBLISHER,
     ErrCode.ILLEGAL_TRANSITION, None),
    ("T16", "静默 44:59 不能走 T16",
     {"last_runner_evidence_at": NOW - 45 * MIN + SEC}, PUBLISHER,
     ErrCode.ILLEGAL_TRANSITION, None),
    # T17：无进度 + 距抢单 ≥45min + 仅发单人
    ("T17", "有进度不能走 T17",
     {"has_runner_evidence": True, "last_runner_evidence_at": NOW - 46 * MIN}, PUBLISHER,
     ErrCode.ILLEGAL_TRANSITION, None),
    ("T17", "距抢单 44:59 不能走 T17", {"has_runner_evidence": False,
                                        "last_runner_evidence_at": None,
                                        "accepted_at": NOW - 45 * MIN + SEC}, PUBLISHER,
     ErrCode.ILLEGAL_TRANSITION, None),
)


def _ctx_for(tid: str, over: dict[str, Any]) -> OrderCtx:
    """按迁移号构造该迁移的合法基准 ctx，再叠加反例覆盖。"""
    base: dict[str, Any] = {}
    if tid == "T1":
        base = {
            "status": Status.PENDING.value, "runner_id": None, "accepted_at": None,
            "has_runner_evidence": False, "last_runner_evidence_at": None,
            "publisher_pending_count": 1,
        }
    elif tid in ("T2", "T3", "T4"):
        base = {
            "status": Status.PENDING.value, "runner_id": None, "accepted_at": None,
            "has_runner_evidence": False, "last_runner_evidence_at": None,
            "deadline": NOW - SEC if tid == "T4" else NOW + 2 * HOUR,
        }
    elif tid in ("T5", "T6", "T7", "T8", "T16", "T17"):
        base = {"status": Status.IN_PROGRESS.value}
        if tid == "T5":
            base["last_node"] = "PICKED_UP"
        if tid == "T7":
            base["last_node"] = "ON_THE_WAY"
        if tid == "T8":
            base["cancel_request_status"] = "APPROVED"
        if tid == "T16":
            base["has_runner_evidence"] = True
            base["last_runner_evidence_at"] = NOW - 46 * MIN
        if tid == "T17":
            base["has_runner_evidence"] = False
            base["last_runner_evidence_at"] = None
            base["accepted_at"] = NOW - 46 * MIN
    elif tid in ("T9", "T10", "T11"):
        base = {
            "status": Status.DELIVERED.value, "delivered_at": NOW - HOUR,
            "auto_confirm_due_at": NOW - SEC if tid == "T10" else NOW + 23 * HOUR,
        }
    elif tid == "T12":
        base = {"status": Status.COMPLETED.value, "completed_at": NOW - HOUR}
    elif tid in ("T13", "T14", "T15"):
        pre = "IN_PROGRESS" if tid == "T13" else "DELIVERED"
        base = {"status": Status.DISPUTED.value, "pre_dispute_status": pre}
    base.update(over)
    return order(**base)


_NEG_IDS = [f"{tid}-{label}" for tid, label, _o, _a, _c, _act in GUARD_NEGATIVES]


@pytest.mark.parametrize(
    "tid,label,over,actor,code,act", GUARD_NEGATIVES, ids=_NEG_IDS
)
def test_c7_guard_negative(
    tid: str,
    label: str,
    over: dict[str, Any],
    actor: Actor,
    code: int,
    act: Action | None,
) -> None:
    """每条迁移的守卫都必须真的拦得住：角色合法→42201，越权→40310，且零副作用。"""
    ctx = _ctx_for(tid, over)
    action = act or next(t for t in sm.TRANSITIONS if t.id == tid).actions[0]
    expect_error(ctx, action, actor, code)


# ---- ⑤b T13/T14 共用键的分派：PRE 决定落到哪一条（不是"哪条先命中"） -------
# ---- ⑤c T5「节点只能顺序推进」的口径：**严格 +1，跳级被拒**（team-lead 裁定） ----
def test_c7c_t5_rejects_skipping_picked_up() -> None:
    """**口径已裁定（team-lead）**：T5 守卫按严格读法 `target == current + 1`。

    PRD T5 守卫原文是「节点只能顺序推进」。此前实现用的是「单调递增」
    (`target > current`)，因此 `last_node=None` 时可以直接点 `ON_THE_WAY`
    （跳过「已取件」），甚至直接 `ON_THE_WAY → DELIVER` 完成整单，
    时间轴上永远没有 PICKED_UP。

    裁定理由（已写进 `state_machine._node_in_order` 的 docstring）：
    「已取件」是接单人自证的关键节点，也是 T16/T17 判 `has_runner_evidence`
    与纠纷举证的证据链基础；允许跳过等于把最细的一环拿掉，而放宽没有任何产品收益
    （忘了点的接单人只需补点一次）。

    本用例因此从「钉住旧现状」改为「钉住新裁定」：跳级必须 42201。
    """
    # 跳级：一个节点都没点，直接点「配送中」→ 拒
    expect_error(_ctx_for("T5", {"last_node": None}), Action.ON_THE_WAY,
                 RUNNER, ErrCode.ILLEGAL_TRANSITION)
    # 跳级：一个节点都没点，直接标送达 → 拒（T7 也拦，这里验 T5 侧不放行）
    expect_error(_ctx_for("T5", {"last_node": None}), Action.DELIVER,
                 RUNNER, ErrCode.ILLEGAL_TRANSITION)
    # 对照：回退一定被拒（这条是硬要求，没有口径歧义）
    expect_error(_ctx_for("T5", {"last_node": "ON_THE_WAY"}), Action.PICKED_UP,
                 RUNNER, ErrCode.ILLEGAL_TRANSITION)


def test_c7c_t5_accepts_each_single_step() -> None:
    """严格顺序推进的两条正例：`None→PICKED_UP`、`PICKED_UP→ON_THE_WAY`。"""
    for last_node, action in ((None, Action.PICKED_UP),
                              ("PICKED_UP", Action.ON_THE_WAY)):
        result, repo = run(_ctx_for("T5", {"last_node": last_node}), action, RUNNER)
        assert result.to_status == Status.IN_PROGRESS.value
        assert len(repo.events) == 1


def test_c7c_t7_checks_only_the_last_node() -> None:
    """T7「已到最后节点」的守卫**只**校验 `last_node == ON_THE_WAY`，不重复校验 PICKED_UP。

    **措辞更正（第二轮）**：上一版这里写的是「PICKED_UP 可缺」，在 T5 收紧为
    `target == current + 1` 之后这句话已经不成立 —— `last_node` 能停在 `ON_THE_WAY`，
    必然是先走过了 `PICKED_UP`。T7 不再校验 PICKED_UP 是**单一事实来源**的取舍
    （顺序由 T5 保证，T7 只看"够不够到最后节点"），不是"允许缺环"。

    真正该由 T7 拦的是「一个节点都没点」和「只到 PICKED_UP」这两种够不到终点的情况。
    """
    guard = sm._last_node_reached  # noqa: SLF001 - QA 探针，直接调实现里的守卫
    assert guard(_ctx_for("T7", {"last_node": None}), RUNNER, NOW) is False
    assert guard(_ctx_for("T7", {"last_node": "PICKED_UP"}), RUNNER, NOW) is False
    assert guard(_ctx_for("T7", {"last_node": "ON_THE_WAY"}), RUNNER, NOW) is True
    # 补一条：T5 严格顺序下，要把 last_node 推到 ON_THE_WAY 必须先过 PICKED_UP
    assert sm._node_in_order(  # noqa: SLF001
        _ctx_for("T5", {"last_node": "PICKED_UP"}), RUNNER, NOW,
        action=Action.ON_THE_WAY,
    ) is True
    assert sm._node_in_order(  # noqa: SLF001
        _ctx_for("T5", {"last_node": None}), RUNNER, NOW, action=Action.ON_THE_WAY,
    ) is False, "T5 若允许跳级，'PICKED_UP 可缺' 就又会变成真的 —— 这里把它钉死"


@pytest.mark.parametrize(
    "pre,expected_tid,expected_status",
    [
        ("IN_PROGRESS", "T13", "IN_PROGRESS"),
        ("DELIVERED", "T14", "DELIVERED"),
        ("COMPLETED", "T14", "COMPLETED"),
    ],
)
def test_c7b_arbitrate_free_dispatch(pre: str, expected_tid: str,
                                     expected_status: str) -> None:
    ctx = _ctx_for("T13", {"pre_dispute_status": pre, "completed_at": NOW - 3 * DAY,
                           "delivered_at": NOW - 3 * DAY, "dispute_count": 1})
    result, repo = run(ctx, Action.ARBITRATE_FREE, ADMIN)
    assert result.transition_id == expected_tid, (
        f"PRE={pre} 应走 {expected_tid}，实得 {result.transition_id}"
    )
    assert result.to_status == expected_status
    assert len(repo.events) == 1


# ---------------------------------------------------------------------------
# C8-1/2. T16 与 T17 在「静默恰好 45 分钟」这一点的硬互斥
# ---------------------------------------------------------------------------
def test_c8_1_with_evidence_45m_t16_passes_t17_fails() -> None:
    """有进度 + 静默 45:00 → 只能投诉（T16 PASS），自助取消必须被拒（T17 FAIL）。"""
    ctx = order(has_runner_evidence=True, last_runner_evidence_at=NOW - 45 * MIN)
    result, repo = run(ctx, Action.DISPUTE_OPENED, PUBLISHER)
    assert result.transition_id == "T16"
    assert result.to_status == Status.DISPUTED.value
    assert len(repo.events) == 1

    expect_error(ctx, Action.CANCEL_LOST, PUBLISHER, ErrCode.ILLEGAL_TRANSITION)


def test_c8_2_without_evidence_45m_t17_passes_t16_fails() -> None:
    """无进度 + 距抢单 45:00 → 只能自助取消（T17 PASS），投诉必须被拒（T16 FAIL）。"""
    ctx = order(
        has_runner_evidence=False,
        last_runner_evidence_at=None,
        accepted_at=NOW - 45 * MIN,
    )
    result, repo = run(ctx, Action.CANCEL_LOST, PUBLISHER)
    assert result.transition_id == "T17"
    assert result.to_status == Status.CANCELLED.value
    assert len(repo.events) == 1

    expect_error(ctx, Action.DISPUTE_OPENED, PUBLISHER, ErrCode.ILLEGAL_TRANSITION)


@pytest.mark.parametrize("status", list(Status))
def test_c8_1b_t16_t17_never_both_pass(status: Status) -> None:
    """同一份事实快照上，T16/T17 不可能同时通过（守卫层互斥，不靠"用过就关"）。"""
    ctx = _ctx_for("T16", {"status": status.value})
    t16 = sm.guard_silent_45m(ctx, PUBLISHER, NOW)
    t17 = sm.guard_no_progress_45m(ctx, PUBLISHER, NOW)
    assert not (t16 and t17), f"状态 {status} 下 T16/T17 同时通过，互斥被破坏"


# ---------------------------------------------------------------------------
# C8-3. 44:59 拒 / 45:00 过（注入 now，不 sleep）
# ---------------------------------------------------------------------------
_BOUNDARY = [
    pytest.param(45 * MIN - SEC, False, id="44m59s-reject"),
    pytest.param(45 * MIN - datetime.timedelta(microseconds=1), False, id="44m59.999999-reject"),
    pytest.param(45 * MIN, True, id="45m00s-pass"),
    pytest.param(45 * MIN + SEC, True, id="45m01s-pass"),
]


@pytest.mark.parametrize("elapsed,expected", _BOUNDARY)
def test_c8_3_t17_boundary(elapsed: datetime.timedelta, expected: bool) -> None:
    """T17：`now - accepted_at` 的 45 分钟边界（>= 通过）。"""
    ctx = order(
        has_runner_evidence=False,
        last_runner_evidence_at=None,
        accepted_at=NOW - elapsed,
    )
    assert sm.guard_no_progress_45m(ctx, PUBLISHER, NOW) is expected


@pytest.mark.parametrize("elapsed,expected", _BOUNDARY)
def test_c8_3_t16_boundary(elapsed: datetime.timedelta, expected: bool) -> None:
    """T16：`now - 最后进度时刻` 的 45 分钟边界（>= 通过）。"""
    ctx = order(has_runner_evidence=True, last_runner_evidence_at=NOW - elapsed)
    assert sm.guard_silent_45m(ctx, PUBLISHER, NOW) is expected


def test_c8_3b_boundary_through_apply_not_guard() -> None:
    """边界从 `apply()` 全链路验一遍（守卫通过但 CAS 之前不能有额外拒绝）。"""
    almost = order(
        has_runner_evidence=False, last_runner_evidence_at=None,
        accepted_at=NOW - 45 * MIN + SEC,
    )
    expect_error(almost, Action.CANCEL_LOST, PUBLISHER, ErrCode.ILLEGAL_TRANSITION)

    exact = order(
        has_runner_evidence=False, last_runner_evidence_at=None,
        accepted_at=NOW - 45 * MIN,
    )
    result, _ = run(exact, Action.CANCEL_LOST, PUBLISHER)
    assert result.transition_id == "T17"


# ---------------------------------------------------------------------------
# C8-4. 接单人自己调 T16 / T17 → 40310 且不产生事件
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action", [Action.DISPUTE_OPENED, Action.CANCEL_LOST],
                         ids=["T16", "T17"])
def test_c8_4_runner_cannot_call_publisher_exits(action: Action) -> None:
    """接单人（RUNNER 角色）调发单人的两条出口 → 403 / 40310，零事件零改状态。"""
    ctx = order(
        has_runner_evidence=False, last_runner_evidence_at=None,
        accepted_at=NOW - 46 * MIN,
    )
    err = expect_error(ctx, action, RUNNER, ErrCode.ORDER_NOT_YOURS)
    assert err.http_status == 403, f"越权必须是 403，实得 {err.http_status}"


@pytest.mark.parametrize("action", [Action.DISPUTE_OPENED, Action.CANCEL_LOST],
                         ids=["T16", "T17"])
def test_c8_4b_insider_role_spoof_still_40310(action: Action) -> None:
    """接单人冒充 `PUBLISHER` 角色（id=200≠publisher_id）→ 42201 而非 40310。

    这是刻意的判定：`Role` 来自 token，正常链路不会出现；
    万一出现，守卫必须仍然拦住（不能因为"角色是发单人"就放行）。
    """
    ctx = order(
        has_runner_evidence=False, last_runner_evidence_at=None,
        accepted_at=NOW - 46 * MIN,
    )
    expect_error(ctx, action, Actor.publisher(200), ErrCode.ILLEGAL_TRANSITION)


# ---------------------------------------------------------------------------
# C8-5. T14 回滚到 DELIVERED：目标态、due_at 重写、delivered_at 不变
# ---------------------------------------------------------------------------
def test_c8_5_t14_restore_delivered() -> None:
    """PRE=DELIVERED → 目标仍是 DELIVERED；due_at 重写为 now+24h；delivered_at 不动。"""
    delivered_3d_ago = NOW - 3 * DAY
    ctx = order(
        status=Status.DISPUTED.value,
        pre_dispute_status=Status.DELIVERED.value,
        delivered_at=delivered_3d_ago,
        auto_confirm_due_at=NOW - 2 * DAY,   # 早已"到期"
        dispute_count=1,
    )
    result, repo = run(ctx, Action.ARBITRATE_FREE, ADMIN)

    assert result.transition_id == "T14"
    assert result.to_status == Status.DELIVERED.value, "不得直接判 COMPLETED（BR-34）"
    patch = dict(result.status_patch)
    assert patch["auto_confirm_due_at"] == NOW + 24 * HOUR, "回滚必须重新起算 24h"
    assert "delivered_at" not in patch, "delivered_at 是只读基准（K-12），不许进 patch"
    assert repo.rows[1]["status"] == Status.DELIVERED.value
    # 若实现误改了 delivered_at，这里会看到 3 天前以外的值
    assert ctx.delivered_at == delivered_3d_ago

    # 回滚后：自动确认**不能**立刻触发（guard 只看 due_at）
    restored = order(status=Status.DELIVERED.value, delivered_at=delivered_3d_ago,
                     auto_confirm_due_at=patch["auto_confirm_due_at"])
    assert sm.guard_auto_confirm_due(restored, SYSTEM, NOW) is False


def test_c8_5b_t14_restore_completed() -> None:
    """PRE=COMPLETED → 回到 COMPLETED，且 pre_dispute_status 必须清空。"""
    ctx = order(
        status=Status.DISPUTED.value,
        pre_dispute_status=Status.COMPLETED.value,
        completed_at=NOW - 3 * DAY,
        dispute_count=1,
    )
    result, _ = run(ctx, Action.ARBITRATE_FREE, ADMIN)
    assert result.to_status == Status.COMPLETED.value
    assert dict(result.status_patch)["pre_dispute_status"] is None


def test_c8_5c_t13_restore_in_progress_keeps_runner_evidence() -> None:
    """PRE=IN_PROGRESS → 回到 IN_PROGRESS，且 T17 仍被 has_progress 永久关闭。"""
    ctx = order(
        status=Status.DISPUTED.value,
        pre_dispute_status=Status.IN_PROGRESS.value,
        has_runner_evidence=True,
        last_runner_evidence_at=NOW - 5 * HOUR,
        dispute_count=1,
    )
    result, _ = run(ctx, Action.ARBITRATE_FREE, ADMIN)
    assert result.to_status == Status.IN_PROGRESS.value
    assert dict(result.status_patch)["pre_dispute_status"] is None
    # 回滚后接单人进度仍在 → 只能再投诉，不能再自助取消；
    # 且 dispute_count=1（BR-28 一单一次）使 T16 也不可再走 → 必须靠协商取消/仲裁
    after = order(status=Status.IN_PROGRESS.value, has_runner_evidence=True,
                  last_runner_evidence_at=NOW - 5 * HOUR, dispute_count=1)
    assert sm.guard_no_progress_45m(after, PUBLISHER, NOW) is False


# ---------------------------------------------------------------------------
# C9. 7 状态 × 19 action × 4 角色全笛卡尔积，逐格调用 apply()
# ---------------------------------------------------------------------------
_PERMISSIVE = {
    # 该状态下"最容易通过"的事实快照：让合法键大多能真的走通
    Status.PENDING: dict(status="PENDING", runner_id=None, accepted_at=None,
                         has_runner_evidence=False, last_runner_evidence_at=None,
                         deadline=NOW - SEC, publisher_pending_count=0,
                         runner_inflight_count=0),
    Status.IN_PROGRESS: dict(status="IN_PROGRESS", accepted_at=NOW - 46 * MIN,
                             has_runner_evidence=True,
                             last_runner_evidence_at=NOW - 46 * MIN,
                             last_node="ON_THE_WAY", deadline=NOW + 2 * HOUR,
                             cancel_request_status="APPROVED"),
    Status.DELIVERED: dict(status="DELIVERED", delivered_at=NOW - 25 * HOUR,
                           auto_confirm_due_at=NOW - HOUR, dispute_count=0),
    Status.COMPLETED: dict(status="COMPLETED", completed_at=NOW - HOUR, dispute_count=0),
    Status.DISPUTED: dict(status="DISPUTED", pre_dispute_status="DELIVERED",
                          delivered_at=NOW - 3 * DAY, auto_confirm_due_at=NOW - DAY),
    Status.CANCELLED: dict(status="CANCELLED"),
    Status.EXPIRED: dict(status="EXPIRED"),
}

_ROLES = (PUBLISHER, RUNNER, ADMIN, SYSTEM)


def _legal_candidates(status: Status, action: Action) -> tuple[sm.Trans, ...]:
    return sm.transition_candidates(status, action)


@pytest.mark.parametrize("status", list(Status), ids=[s.value for s in Status])
@pytest.mark.parametrize("action", list(Action), ids=[a.value for a in Action])
def test_c9_cartesian(status: Status, action: Action) -> None:
    """全笛卡尔积：每一格的错误码与副作用都必须符合约定。

    - 非合法键 → 42201
    - 合法键但角色不在 actors → 40310
    - 合法键 + 角色合法 → 要么成功（恰好 1 条事件 + 状态到位），要么 42201（守卫拒）
    - **任何被拒绝的分支都不得留下事件或改动状态**（不能"抛了错却脏了数据"）
    """
    cands = _legal_candidates(status, action)
    base = dict(_PERMISSIVE[status])

    for actor in _ROLES:
        ctx = order(**base)
        repo = MemoryOrderRepo()
        repo.seed(ctx.id, ctx.status)

        if not cands:
            with pytest.raises(ApiError) as exc:
                sm.apply(repo, ctx, action, actor, now=NOW)
            assert exc.value.code == ErrCode.ILLEGAL_TRANSITION == 42201, (
                f"[{status.value} × {action.value} × {actor.role.value}] "
                f"非法键必须是 42201，实得 {exc.value.code}"
            )
            assert repo.events == []
            assert repo.rows[ctx.id]["status"] == status.value
            continue

        if not any(actor.role in c.actors for c in cands):
            with pytest.raises(ApiError) as exc:
                sm.apply(repo, ctx, action, actor, now=NOW)
            assert exc.value.code == ErrCode.ORDER_NOT_YOURS == 40310, (
                f"[{status.value} × {action.value} × {actor.role.value}] "
                f"越权必须是 40310，实得 {exc.value.code}"
            )
            assert exc.value.http_status == 403
            assert repo.events == [], "越权被拒却写了事件"
            assert repo.rows[ctx.id]["status"] == status.value
            continue

        try:
            result = sm.apply(repo, ctx, action, actor, now=NOW)
        except ApiError as err:
            assert err.code == ErrCode.ILLEGAL_TRANSITION, (
                f"[{status.value} × {action.value} × {actor.role.value}] "
                f"守卫拒绝必须是 42201，实得 {err.code}"
            )
            assert repo.events == [], (
                f"[{status.value} × {action.value} × {actor.role.value}] "
                f"守卫拒绝却写了 {len(repo.events)} 条事件"
            )
            assert repo.rows[ctx.id]["status"] == status.value, (
                f"[{status.value} × {action.value} × {actor.role.value}] 守卫拒绝却改了状态"
            )
            continue

        assert len(repo.events) == 1, "成功迁移必须且只能写 1 条事实"
        assert repo.rows[ctx.id]["status"] == result.to_status
        assert repo.events[0][0].from_status == status.value
        assert repo.events[0][0].to_status == result.to_status


def test_c9_matrix_shape() -> None:
    """矩阵规模自检：7 × 19 = 133 个 (状态, 动作) 格，合法 27 个（不含 T1 的 None 键）。"""
    all_pairs = {(s.value, a) for s in Status for a in Action}
    assert len(all_pairs) == 133
    legal = {
        (k[0], k[1].value) for k in sm.LEGAL_TRANSITION_KEYS if k[0] is not None
    }
    assert len(legal) == 27, f"合法键应有 27 个（28 减去 T1 的 None 键），实得 {len(legal)}"
    assert legal <= all_pairs
    # 非法格 106 个，全部要被 42201 覆盖
    assert len(all_pairs - legal) == 106


def test_c9_create_not_reachable_through_apply() -> None:
    """已知设计约束：T1 的 from_status 是 None，`apply()` 永远走不到它。

    这里把它钉成用例，避免以后有人以为 `apply(order, CREATE)` 能建单。
    """
    assert sm.transition_candidates(None, Action.CREATE)[0].id == "T1"
    for status in Status:
        assert sm.transition_candidates(status, Action.CREATE) == ()
        ctx = order(status=status.value)
        expect_error(ctx, Action.CREATE, PUBLISHER, ErrCode.ILLEGAL_TRANSITION)


# ---------------------------------------------------------------------------
# C10. 抗"假通过"：patch 的键必须是真实 DB 列
# ---------------------------------------------------------------------------
def test_c10_patch_keys_are_real_columns() -> None:
    """`MemoryOrderRepo` 不校验列名，写错一个键在内存里"照样通过"、上真库就炸。

    所以这里把 17 条迁移能产出的 patch 键全部收敛，逐个比对 `orders` 的真实列。
    """
    from app.models.order import Order

    real_columns = set(Order.__table__.columns.keys())
    produced: set[str] = set()

    cases: list[tuple[OrderCtx, Action, Actor]] = [
        (_ctx_for("T2", {}), Action.ACCEPT, RUNNER),
        (_ctx_for("T7", {}), Action.DELIVER, RUNNER),
        (_ctx_for("T9", {}), Action.CONFIRM, PUBLISHER),
        (_ctx_for("T10", {}), Action.AUTO_CONFIRM, SYSTEM),
        (_ctx_for("T11", {}), Action.DISPUTE_OPENED, PUBLISHER),
        (_ctx_for("T12", {}), Action.DISPUTE_OPENED, PUBLISHER),
        (_ctx_for("T13", {}), Action.ARBITRATE_FREE, ADMIN),
        (_ctx_for("T14", {}), Action.ARBITRATE_FREE, ADMIN),
        (_ctx_for("T15", {}), Action.ARBITRATE_LIABLE, ADMIN),
        (_ctx_for("T16", {}), Action.DISPUTE_OPENED, PUBLISHER),
        (_ctx_for("T17", {}), Action.CANCEL_LOST, PUBLISHER),
        (_ctx_for("T3", {}), Action.CANCEL, PUBLISHER),
    ]
    for ctx, action, actor in cases:
        payload = TransitionPayload(reason="QA 复现用原因")
        result, _ = run(ctx, action, actor, payload=payload)
        produced |= set(result.status_patch)

    assert produced, "居然没有产出任何 patch，用例写错了"
    unknown = produced - real_columns
    assert not unknown, f"patch 里出现 orders 表没有的列（上真库会 IntegrityError）：{unknown}"

    # 值域也要能塞进列宽（pre_dispute_status 是 String(12)）
    for ctx, action, actor in cases:
        result, _ = run(ctx, action, actor)
        for key, value in result.status_patch.items():
            if isinstance(value, str) and key in ("pre_dispute_status", "cancel_reason"):
                assert len(value) <= 12 if key == "pre_dispute_status" else True


def test_c10_status_values_fit_varchar12() -> None:
    """所有目标状态字符串都必须 ≤ 12 字符（DB 列是 varchar(12)）。"""
    for trans in sm.TRANSITIONS:
        if isinstance(trans.to_status, Status):
            assert len(trans.to_status.value) <= 12, trans.id
    for status in Status:
        assert len(status.value) <= 12, status.value


def test_c10_action_values_fit_varchar20() -> None:
    """19 个 action 都必须 ≤ 20 字符（order_progress.action 是 varchar(20)）。

    19 = 5.4 正文的 18 个 + 补入的 `URGE`（催单需可审计事实，FR-FULFILL-05）。
    """
    assert len(sm.PROGRESS_ACTIONS) == 19
    for action in sm.PROGRESS_ACTIONS:
        assert len(action.value) <= 20, action.value


def test_c10_runner_evidence_matches_doc() -> None:
    """7.4 唯一定义：RUNNER_EVIDENCE 必须逐字是这四个 action。"""
    assert sm.RUNNER_EVIDENCE == ("PICKED_UP", "ON_THE_WAY", "DELIVER", "NOTE")
    # 发单人的催单 / 协商取消不得算进度
    for forbidden in ("URGE", "REQUEST_CANCEL", "REJECT_CANCEL", "CONFIRM",
                      "CANCEL", "CREATE", "ACCEPT"):
        assert forbidden not in sm.RUNNER_EVIDENCE


def test_c10_runner_evidence_requires_actor_match() -> None:
    """发单人的 NOTE 不算进度（否则发单人自己关死 T17 出口）。"""
    facts = [
        sm.ProgressFact(action="NOTE", actor_id=100, created_at=NOW - HOUR),      # 发单人催单
        sm.ProgressFact(action="ACCEPT", actor_id=200, created_at=NOW - 3 * HOUR),
    ]
    has, since = sm.probe_progress(
        facts, runner_id=200, accepted_at=NOW - 3 * HOUR
    )
    assert has is False
    assert since == NOW - 3 * HOUR

    facts.append(sm.ProgressFact(action="PICKED_UP", actor_id=200, created_at=NOW - 10 * MIN))
    has2, since2 = sm.probe_progress(
        facts, runner_id=200, accepted_at=NOW - 3 * HOUR
    )
    assert has2 is True
    assert since2 == NOW - 10 * MIN
