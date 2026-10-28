"""列宽自检（**纯模型层，不需要数据库**，进默认套件）。

## 为什么需要这个文件

真库上曾经炸过三个"文档写错、只有真库能暴露"的列宽缺陷：

| 列 | 原宽 | 最长取值 | 后果 |
|---|---|---|---|
| `orders.settle_state` | varchar(8) | `'UNSETTLED'`(9) | **它是 DEFAULT** → 任何 `insert into orders` 立即失败 |
| `order_progress.actor_role` | varchar(8) | `'PUBLISHER'`(9) | 发单人产生的进度事件全部写不进去 |
| `users.credit_level` | varchar(8) | `'EXCELLENT'`(9) | GENERATED 列，分数 ≥110 的用户一插入就炸 |

当时 **524 个单测全绿** —— 因为 `MemoryOrderRepo` 是内存替身，它不校验列宽。
这就是"替身假通过"：**单元测试能证明逻辑对不对，证明不了 Schema 装不装得下**。

本文件把"装不装得下"变成纯模型层的断言，不依赖 PG，因此每次 `pytest tests` 都会跑：

1. `test_enum_value_fits_column_width` —— 每个枚举列的最长允许值 ≤ 列宽（核心回归）；
2. `test_every_string_column_is_classified` —— **新增 String 列必须被登记**
   （枚举列或自由文本列），漏登记即 fail。这是防止同类问题复发的闸门：
   没有它，下一个人加列时照样不会想到宽度；
3. `test_registry_has_no_stale_entry` —— 登记表里的键必须真实存在（防改名后失配）；
4. `test_credit_level_values_come_from_computed_expr` —— 登记值必须与
   `CREDIT_LEVEL_EXPR` 的 SQL 文本一致，避免"登记表自己写错、测试跟着错"；
5. `test_tight_enum_columns_headroom` —— 把当前余量钉成快照：新枚举值如果吃掉了
   某个列的剩余宽度，这条会 fail 并提示"该加宽了"，而不是等到上线炸。

不覆盖：`Text()`（无宽度）列的宽度（本来就没有上限），只要求它们被登记为自由文本。
"""

from __future__ import annotations

import re

import pytest
from app.models import Base
from app.models.dispute import DISPUTE_REASONS, DISPUTE_RESULTS, DISPUTE_STATUSES
from app.models.governance import AUDIT_ACTIONS, AUDIT_TARGET_TYPES
from app.models.order import (
    ACTOR_ROLES,
    CANCEL_REQUEST_STATUSES,
    ORDER_STATUSES,
    ORDER_TYPES,
    PROGRESS_ACTIONS,
    SETTLE_STATES,
)
from app.models.poi import POI_TYPES
from app.models.push import NOTIFICATION_KINDS, PUSH_CHANNELS
from app.models.review import CREDIT_REASON_CODES, REVIEW_DIRECTIONS
from app.models.user import CREDIT_LEVEL_EXPR, USER_STATUSES, VERIFY_STATUSES
from app.modules.order.state_machine import Action, Status
from sqlalchemy import CheckConstraint, String

# ---------------------------------------------------------------------------
# 枚举列登记表（只登记"没有 CHECK 约束可自动解析"的那些）
# ---------------------------------------------------------------------------
#: 有 CHECK 约束的枚举列会被 `_enum_values_from_check()` 自动发现，**不要**在这里重复登记
#: （重复登记会有两处口径，迟早漂移；`test_symbol_sources_are_not_duplicated` 会拦）。
ENUM_COLUMNS: dict[str, tuple[str, ...]] = {
    # GENERATED 列：最阴的一处 —— 任何 `CHECK (col IN ...)` 都扫不到它，
    # 只有"真的插一个信誉分 ≥110 的用户"才会炸。
    "users.credit_level": tuple(
        re.findall(r"THEN '([A-Z_]+)'", CREDIT_LEVEL_EXPR)
    )
    + tuple(re.findall(r"ELSE '([A-Z_]+)'", CREDIT_LEVEL_EXPR)),
    # 挂起态：取值域就是订单状态全集（5.2 只说"投诉前状态"，没建 CHECK）
    "orders.pre_dispute_status": tuple(s.value for s in Status),
    # 事件流的迁移前后状态：同样没建 CHECK，但宽度一样会被真库拦
    "order_progress.from_status": tuple(s.value for s in Status),
    "order_progress.to_status": tuple(s.value for s in Status),
    # 审计与通知的取值集合在应用层常量里，DB 层没有 CHECK
    "admin_audit_logs.target_type": AUDIT_TARGET_TYPES,
    "admin_audit_logs.action": AUDIT_ACTIONS,
    "notifications.kind": NOTIFICATION_KINDS,
    # 供断言用（值不来自本表，仅用于交叉校验状态机枚举与 DB 常量一致）
    "_crosscheck.orders.status": ORDER_STATUSES,
    "_crosscheck.order_progress.action": PROGRESS_ACTIONS,
    "_crosscheck.order_progress.actor_role": ACTOR_ROLES,
    "_crosscheck.orders.settle_state": SETTLE_STATES,
    "_crosscheck.pois.type": POI_TYPES,
    "_crosscheck.users.verify_status": VERIFY_STATUSES,
    "_crosscheck.users.status": USER_STATUSES,
    "_crosscheck.auth_identities.provider": ("PHONE", "WECHAT"),
    "_crosscheck.orders.type": ORDER_TYPES,
    "_crosscheck.reviews.direction": REVIEW_DIRECTIONS,
    "_crosscheck.credit_logs.reason_code": CREDIT_REASON_CODES,
    "_crosscheck.disputes.reason": DISPUTE_REASONS,
    "_crosscheck.disputes.status": DISPUTE_STATUSES,
    "_crosscheck.disputes.result": DISPUTE_RESULTS,
    "_crosscheck.order_cancel_requests.status": CANCEL_REQUEST_STATUSES,
    "_crosscheck.push_logs.channel": PUSH_CHANNELS,
}

#: 自由文本列（不是枚举，宽度只需装得下人类输入）。
#: **这张表的存在意义**：新增一个 String 列时，`test_every_string_column_is_classified`
#: 会要求你先回答"它是枚举还是自由文本"，从而不可能悄悄加一个宽度不够的枚举列。
FREE_TEXT_COLUMNS: frozenset[str] = frozenset(
    {
        "pois.name",
        "sys_configs.key",
        "users.student_no",
        "users.nickname",
        "users.avatar_url",            # Text()，无宽度上限
        "users.phone_mask",
        # bcrypt 哈希（0003 新增）：**不是枚举**，宽度只需装得下 60 字符的
        # `$2b$12$...`（给到 128 是为了将来换 argon2id）。它不可逆，所以宽度
        # 不足的后果是"哈希被截断 ⇒ 所有人都登录不上"，而不是"值写不进去"。
        "users.password_hash",
        "users.dept",
        "users.grade",
        "admin_audit_logs.note",
        "auth_identities.provider_uid",
        "notifications.title",
        "notifications.body",
        "orders.order_no",
        "orders.description",
        "orders.cancel_reason",
        "student_verifications.student_no_input",
        "student_verifications.name_input",
        "student_verifications.email",
        "student_verifications.evidence_key",  # Text()，无宽度上限
        "student_verifications.reject_reason",
        "credit_logs.note",
        "disputes.result_note",
        "order_progress.note",
        "reviews.content",
    }
)


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------
def _string_columns() -> dict[str, tuple[str, int | None]]:
    """`"table.column" -> (table, width)`；覆盖所有 `String` 子类（含 `Text`，其 width=None）。"""
    out: dict[str, tuple[str, int | None]] = {}
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, String):
                out[f"{table.name}.{column.name}"] = (table.name, column.type.length)
    return out


def _enum_values_from_check() -> dict[str, tuple[str, ...]]:
    """从模型的 `CHECK (col IN (...))` 里自动提取枚举取值。

    支持两种写法（项目里两种都有）：
    - `status IN ('A', 'B')`
    - `result IS NULL OR result IN ('A', 'B')`
    """
    found: dict[str, tuple[str, ...]] = {}
    pattern = re.compile(r"(?:\w+ IS NULL OR )?(\w+) IN \((?P<vals>[^)]*)\)")
    for table in Base.metadata.sorted_tables:
        for constraint in table.constraints:
            if not isinstance(constraint, CheckConstraint):
                continue
            matched = pattern.fullmatch(str(constraint.sqltext))
            if matched:
                values = tuple(re.findall(r"'([^']*)'", matched.group("vals")))
                found[f"{table.name}.{matched.group(1)}"] = values
    return found


CHECK_ENUM_COLUMNS = _enum_values_from_check()
ALL_ENUM_COLUMNS: dict[str, tuple[str, ...]] = {**CHECK_ENUM_COLUMNS, **{
    k: v for k, v in ENUM_COLUMNS.items() if not k.startswith("_crosscheck.")
}}


# ---------------------------------------------------------------------------
# ① 核心回归：枚举值必须装得下
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key,values",
    sorted(ALL_ENUM_COLUMNS.items()),
    ids=sorted(ALL_ENUM_COLUMNS),
)
def test_enum_value_fits_column_width(key: str, values: tuple[str, ...]) -> None:
    """每个枚举列的**最长允许值**必须 ≤ 列宽。

    真库报错是 `value too long for type character varying(n)`，指向列宽而不指向
    业务规则 —— 在 GENERATED 列上尤其难查。这条断言把它提前到单测阶段。
    """
    table_name, width = _string_columns()[key]
    assert width is not None, f"{key} 是枚举列但用无宽度的 Text，请改回 String(n)"
    longest = max(values, key=len)
    assert len(longest) <= width, (
        f"{table_name}.{key.split('.', 1)[1]} 列宽 {width} 装不下枚举值 {longest!r}"
        f"（{len(longest)} 字符）。请放宽该列（建议 16）并同步迁移 0001。"
    )


def test_the_three_known_defects_stay_fixed() -> None:
    """把真库炸过的三处钉死（防止有人"优化"回 varchar(8)）。"""
    columns = _string_columns()
    for key, value in (
        ("orders.settle_state", "UNSETTLED"),
        ("order_progress.actor_role", "PUBLISHER"),
        ("users.credit_level", "EXCELLENT"),
    ):
        width = columns[key][1]
        assert width is not None and width >= len(value), (
            f"{key} 宽度 {width} < {len(value)}（{value!r}），历史缺陷回归"
        )
        assert width >= 16, f"{key} 建议保持 varchar(16) 以留余量，当前 {width}"


# ---------------------------------------------------------------------------
# ② 闸门：新增 String 列必须被登记
# ---------------------------------------------------------------------------
def test_every_string_column_is_classified() -> None:
    """所有 String 列必须被"分类"：枚举列（有 CHECK / 已登记）或自由文本列。

    未登记的列会让本用例 fail —— 这是刻意设计的"强制思考"：
    加列的人必须回答"这列是枚举还是自由文本"，枚举列随后会被上一条用例校验宽度。
    """
    columns = set(_string_columns())
    classified = set(ALL_ENUM_COLUMNS) | set(FREE_TEXT_COLUMNS)
    unclassified = columns - classified
    assert not unclassified, (
        "以下 String 列既不是已登记的枚举列、也不在 FREE_TEXT_COLUMNS 里：\n  "
        + "\n  ".join(sorted(unclassified))
        + "\n→ 枚举列请加到 ENUM_COLUMNS（并确认列宽装得下最长取值）；"
        "\n→ 自由文本请加到 FREE_TEXT_COLUMNS。"
    )


def test_registry_has_no_stale_entry() -> None:
    """登记表里的键必须真实存在（列改名/删列后，登记表不能留幽灵条目）。"""
    columns = set(_string_columns())
    for registry_name, keys in (
        ("ENUM_COLUMNS", {k for k in ENUM_COLUMNS if not k.startswith("_crosscheck.")}),
        ("FREE_TEXT_COLUMNS", set(FREE_TEXT_COLUMNS)),
    ):
        stale = keys - columns
        assert not stale, f"{registry_name} 里有不存在的列（改名或删列了？）：{sorted(stale)}"


def test_symbol_sources_are_not_duplicated() -> None:
    """同一个枚举列不能既靠 CHECK 自动解析、又在 ENUM_COLUMNS 里手工登记。"""
    both = set(CHECK_ENUM_COLUMNS) & set(ENUM_COLUMNS)
    assert not both, (
        f"这些列同时有 CHECK 约束和手工登记，两处口径迟早漂移，请删掉手工登记：{sorted(both)}"
    )


def test_text_columns_are_declared_free_text() -> None:
    """无宽度列（Text）也必须登记为自由文本 —— 它们不参与宽度校验，但不能凭空出现。"""
    columns = _string_columns()
    unbounded = {key for key, (_t, width) in columns.items() if width is None}
    assert unbounded <= FREE_TEXT_COLUMNS, (
        f"无宽度的 Text 列未登记为自由文本：{sorted(unbounded - FREE_TEXT_COLUMNS)}"
    )


# ---------------------------------------------------------------------------
# ③ 登记值不能自己写错：与 SQL / 状态机枚举交叉校验
# ---------------------------------------------------------------------------
def test_credit_level_values_come_from_computed_expr() -> None:
    """`users.credit_level` 的登记值必须与 `CREDIT_LEVEL_EXPR` 的 SQL 文本一致。

    否则"登记表写错 + 测试跟着错"会一起骗过所有人（本次缺陷的同类风险）。
    """
    registered = set(ENUM_COLUMNS["users.credit_level"])
    declared = set(re.findall(r"'([A-Z_]+)'", CREDIT_LEVEL_EXPR))
    assert registered == declared, (
        f"credit_level 登记值 {sorted(registered)} 与 CREDIT_LEVEL_EXPR 的 {sorted(declared)} 不一致"
    )
    assert registered == {"EXCELLENT", "GOOD", "NORMAL", "LIMITED"}  # BR-26 四档


@pytest.mark.parametrize(
    "key,expected",
    [
        ("_crosscheck.orders.status", {s.value for s in Status}),
        ("_crosscheck.order_progress.action", {a.value for a in Action}),
    ],
)
def test_state_machine_matches_db_constants(key: str, expected: set[str]) -> None:
    """DB 层常量与状态机枚举逐字一致（两套枚举漂移会导致"状态机放行、DB 插不进"）。"""
    assert set(ENUM_COLUMNS[key]) == expected


def test_status_and_action_enum_sizes() -> None:
    """规模快照：7 个状态、19 个 action（18 + 补入的 URGE）。"""
    assert len(Status) == 7
    assert len(Action) == 19
    assert len(ENUM_COLUMNS["orders.pre_dispute_status"]) == 7


# ---------------------------------------------------------------------------
# ④ 余量快照：新枚举值吃掉剩余宽度时要先失败
# ---------------------------------------------------------------------------
#: 当前"紧"的枚举列余量。新枚举值一旦让余量变化，这条断言会 fail，
#: 迫使改动者显式确认"这个宽度还够不够"，而不是等到真库上报 StringDataRightTruncation。
#:
#: 2025-xx 第二轮放宽后（team-lead 裁定"余量吃紧的列一次性排掉"）：
#: 7 个"每次写都要碰"的列统一到 varchar(16)，本表随之更新。
HEADROOM_BUDGET: dict[str, int] = {
    # 最长值 IN_PROGRESS(11) / varchar(16)
    "orders.status": 5,
    # **每条进度事件都要写这两列** —— 本轮从 12 放宽到 16，余量 1 → 5
    "order_progress.from_status": 5,
    "order_progress.to_status": 5,
    # 挂起态存的是订单状态，与 orders.status 同宽
    "orders.pre_dispute_status": 5,
    # 最长值 FOR_PUBLISHER(13) / varchar(16) —— 本题库里余量最小的一列（3）
    "disputes.result": 3,
    # 最长值 APPROVED(8) / varchar(16)
    "order_cancel_requests.status": 8,
    # 最长值 ARBITRATE_LIABLE(16) / varchar(24) —— 第二轮从 20 放宽，余量 4 → 8
    "order_progress.action": 8,
    # 第一轮修复（varchar(8) 装不下枚举值）
    "orders.settle_state": 7,
    "order_progress.actor_role": 7,
    "users.credit_level": 7,
}


@pytest.mark.parametrize("key,expected", sorted(HEADROOM_BUDGET.items()), ids=sorted(HEADROOM_BUDGET))
def test_tight_enum_columns_headroom(key: str, expected: int) -> None:
    """余量 = 列宽 − 最长枚举值长度。变化即失败，提示同步加宽列。"""
    width = _string_columns()[key][1]
    assert width is not None
    longest = max(ALL_ENUM_COLUMNS[key], key=len)
    headroom = width - len(longest)
    assert headroom == expected, (
        f"{key} 余量从 {expected} 变成 {headroom}（列宽 {width}，最长值 {longest!r}）。"
        f"如果是新增了更长的枚举值：请同步加宽该列并更新迁移 0001 与本快照。"
    )
    assert headroom >= 1, f"{key} 没有余量了，任何更长的枚举值都会让真库插入失败"


def test_no_enum_column_has_zero_headroom() -> None:
    """兜底：所有枚举列都必须至少有 1 个字符余量（否则等于"刚好卡死"）。"""
    tight = {
        key: width - max(len(v) for v in values)
        for key, values in ALL_ENUM_COLUMNS.items()
        for width in [_string_columns()[key][1] or 0]
        if width - max(len(v) for v in values) < 1
    }
    assert not tight, f"这些枚举列的余量不足 1：{tight}"


def test_min_enum_headroom_is_reported() -> None:
    """把当前**最小余量**与其所在列钉下来（这是"下一颗雷"的位置）。

    第二轮放宽后最小余量是 3，出现在 `disputes.result`（`FOR_PUBLISHER`=13 / varchar(16)）。
    这个数字不是"越大越好"的指标，而是**下一颗雷的位置**：M2 若给 `disputes.result`
    加一个 17+ 字符的取值，就会先在真库上报 `StringDataRightTruncation`。
    """
    headrooms = {
        key: (_string_columns()[key][1] or 0) - max(len(v) for v in values)
        for key, values in ALL_ENUM_COLUMNS.items()
    }
    narrowest = min(headrooms.values())
    narrowest_keys = sorted(k for k, v in headrooms.items() if v == narrowest)
    assert narrowest == 3, (
        f"最小余量从 3 变成 {narrowest}（列：{narrowest_keys}）。"
        "变小时说明加了更长的枚举值，请同步加宽列与迁移 0001。"
    )
    assert narrowest_keys == ["disputes.result", "push_logs.channel"], (
        f"最小余量所在列变化：{narrowest_keys}"
    )


def test_phone_mask_has_room_for_future_formats() -> None:
    """`phone_mask` 不是枚举列，但它曾经余量为 **0**（`138****8888` 恰好 11 字符）。

    余量 0 等于把掩码格式焊死成"3-4-4"：改一次规则就炸，所以一并放宽到 16。
    """
    width = _string_columns()["users.phone_mask"][1]
    assert width is not None and width >= 16, f"users.phone_mask 宽度 {width}，应 ≥16"
    assert width - len("138****8888") >= 5
