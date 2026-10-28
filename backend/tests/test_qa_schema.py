"""QA 独立验证（Edward）：T-03 数据模型与索引（表/列/索引，全部对账 5.2 / 5.3）。

不连库：只读 `Base.metadata`。DDL 侧（`alembic upgrade head --sql` 的
`CREATE TABLE` / `CREATE INDEX` / `WITH TIME ZONE` 计数）在报告里以命令输出为准，
本文件负责 ORM 定义与文档的一致性，两者互为交叉验证。
"""

from __future__ import annotations

import pytest
from app.models import Base
from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import JSONB

# ---------------------------------------------------------------------------
# 5.2 表结构：文档列集合（人工抄录；"dept / grade"、"created_at / updated_at"
# 这种合写行按两个列展开）
# ---------------------------------------------------------------------------
DOC_COLUMNS: dict[str, set[str]] = {
    "users": {
        "id", "student_no", "nickname", "avatar_url", "phone_enc", "phone_mask",
        "dept", "grade", "verify_status", "credit_score", "credit_level", "status",
        "token_version", "is_runner_mode", "notify_settings", "created_at", "updated_at",
    },
    "auth_identities": {"id", "user_id", "provider", "provider_uid"},
    "student_verifications": {
        "id", "user_id", "method", "student_no_input", "name_input", "email",
        "evidence_key", "status", "reject_reason", "reviewer_id", "reviewed_at",
        "created_at",
    },
    "pois": {"id", "campus_id", "name", "type", "lat", "lng", "is_active", "sort_hint"},
    "orders": {
        "id", "order_no", "publisher_id", "runner_id", "type", "from_poi_id", "to_poi_id",
        "from_lat", "from_lng", "to_lat", "to_lng", "reward", "description", "images",
        "pick_code_enc", "expect_minutes", "deadline", "status", "settle_state",
        "push_round", "last_push_at", "is_late", "auto_confirmed", "auto_confirm_due_at",
        "accepted_at", "delivered_at", "completed_at", "cancel_reason",
        "pre_dispute_status", "version", "created_at",
    },
    "order_progress": {
        "id", "order_id", "seq", "action", "from_status", "to_status", "actor_id",
        "actor_role", "note", "images", "created_at",
    },
    "order_cancel_requests": {"id", "order_id", "requester_id", "status"},
    "reviews": {
        "id", "order_id", "from_id", "to_id", "direction", "rating", "tags", "content",
        "is_hidden", "created_at",
    },
    "credit_logs": {
        "id", "user_id", "delta", "score_after", "reason_code", "order_id", "operator_id",
        "note",
    },
    "disputes": {
        "id", "order_id", "initiator_id", "reason", "evidence", "status", "result",
        "result_note", "arbitrator_id", "resolved_at",
    },
    "push_logs": {
        "id", "order_id", "runner_id", "round", "channel", "distance_m", "score",
        "is_clicked", "is_grabbed", "created_at",
    },
    "notifications": {"id", "user_id", "kind", "title", "body", "payload", "read_at"},
    "sys_configs": {"key", "value", "updated_by", "updated_at", "version"},
    "admin_audit_logs": {
        "id", "admin_id", "target_type", "target_id", "action", "before", "after", "note",
        "created_at",
    },
}

#: 5.2 没写、但实现额外加的列：只允许审计时间列（不改变任何业务语义）
AUDIT_ONLY = {"created_at", "updated_at"}

#: 5.2 之外**刻意引入**的结构偏离，必须在这里显式登记（表名 → 列集合）。
#:
#: 语义是「**必须存在**的偏离」，不是「允许出现」：
#: 登记在此意味着 ① 它不属于 5.2（文档基线保持纯洁）② 它必须真的在库里
#: （`0003` 在 head，少了就是迁移漏跑，必须红）。
#:
#: 为什么不直接写进 `DOC_COLUMNS`：`DOC_COLUMNS` 是 `docs/02-ARCHITECTURE.md` 5.2
#: 的逐行转录，是"文档真理"的**唯一副本**。把实现的偏离塞进去，这份基线就不再忠实
#: （以后谁想比对"文档到底写了什么"会读到假信息），偏离记录也会从结构化数据退化成注释。
#: 分开两处后检测力**完全一致**（删列红、加列红、文档列缺失红），但语义不再混淆。
ALLOWED_DEVIATIONS: dict[str, set[str]] = {
    # 0003_add_password_hash（T-07 简化认证）：bcrypt 哈希，5.2 无此列。
    "users": {"password_hash"},
}

# ---------------------------------------------------------------------------
# 5.3 索引清单：表 → {索引名: (是否唯一, WHERE 子句片段或 None)}
# ---------------------------------------------------------------------------
DOC_INDEXES: dict[str, dict[str, tuple[bool, str | None]]] = {
    "users": {
        "uq_users_student_no": (True, None),
        "ix_users_verify_status_created_at": (False, None),
    },
    "auth_identities": {
        "uq_auth_identities_provider_provider_uid": (True, None),
    },
    "student_verifications": {
        "ix_student_verifications_user_id_created_at": (False, None),
        "ix_student_verifications_status_created_at": (False, None),
    },
    "pois": {
        # 5.2 的 UNIQUE(campus_id, name)
        "uq_pois_campus_id_name": (True, None),
        # 5.3 的字典列表/筛选
        "ix_pois_campus_id_is_active_type": (False, None),
    },
    "orders": {
        "ix_orders_status_deadline": (False, "status = 'PENDING'"),
        "ix_orders_publisher_id_created_at_id": (False, None),
        "ix_orders_runner_id_status_created_at_id": (False, None),
        "ix_orders_runner_id_completed_at": (False, "status = 'COMPLETED'"),
        "ix_orders_auto_confirm_due_at": (False, "status = 'DELIVERED'"),
        "uq_orders_order_no": (True, None),
        "ix_orders_created_at": (False, None),
        "ix_orders_accepted_at": (False, "status = 'IN_PROGRESS'"),
    },
    "order_progress": {
        "uq_order_progress_order_id_seq": (True, None),
    },
    "order_cancel_requests": {
        "uq_order_cancel_requests_order_id_pending": (True, "status = 'PENDING'"),
    },
    "reviews": {
        "uq_reviews_order_id_from_id": (True, None),
        "ix_reviews_to_id_created_at": (False, None),
    },
    "credit_logs": {
        "ix_credit_logs_user_id_created_at": (False, None),
    },
    "disputes": {
        "uq_disputes_order_id": (True, None),
        "ix_disputes_status_created_at": (False, None),
    },
    "push_logs": {
        "ix_push_logs_runner_id_created_at": (False, None),
        "ix_push_logs_order_id_round": (False, None),
    },
    "notifications": {
        "ix_notifications_user_id_read_at_created_at": (False, None),
    },
    "admin_audit_logs": {
        "ix_admin_audit_logs_target_type_target_id_created_at": (False, None),
        "ix_admin_audit_logs_admin_id_created_at": (False, None),
    },
}


def _where_of(index) -> str | None:
    opts = index.dialect_options.get("postgresql", {})
    where = opts.get("where")
    if where is None:
        return None
    return " ".join(str(where).split())


# ---------------------------------------------------------------------------
# A1. 14 张表，表名集合与 5.2 完全一致
# ---------------------------------------------------------------------------
def test_a1_exactly_fourteen_tables() -> None:
    actual = set(Base.metadata.tables)
    expected = set(DOC_COLUMNS)
    assert len(actual) == 14, f"表数应为 14，实得 {len(actual)}：{sorted(actual)}"
    assert actual == expected, f"多出 {sorted(actual - expected)}，缺少 {sorted(expected - actual)}"


# ---------------------------------------------------------------------------
# A2. 逐表列集合对账
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("table_name", sorted(DOC_COLUMNS))
def test_a2_columns_match_doc(table_name: str) -> None:
    """文档列必须全在；实现多出来的只能是审计时间列或已登记的刻意偏离。

    三条断言合起来把三个方向都堵住：
    1. `missing`（5.2 要求但不存在）→ 红
    2. `extra`（两边都没登记的多余列）→ 红
    3. `expected`（登记为刻意偏离但实际不存在）→ 红
    """
    actual = set(Base.metadata.tables[table_name].columns.keys())
    doc = DOC_COLUMNS[table_name]
    expected = ALLOWED_DEVIATIONS.get(table_name, set())
    allowed = AUDIT_ONLY | expected

    missing = doc - actual
    extra = actual - doc
    assert not missing, f"{table_name} 缺少 5.2 要求的列：{sorted(missing)}"
    assert extra <= allowed, (
        f"{table_name} 出现未登记的列 {sorted(extra - allowed)}；"
        f"若是刻意的结构偏离，请加到 ALLOWED_DEVIATIONS"
    )
    absent = expected - actual
    assert not absent, (
        f"{table_name} 登记了刻意偏离 {sorted(absent)} 但库里没有 —— "
        f"迁移漏跑还是列被删了？"
    )


def test_a2_orders_has_31_columns() -> None:
    """任务书明确要求：orders 必须 31 列。"""
    cols = Base.metadata.tables["orders"].columns
    assert len(cols) == 31, f"orders 应 31 列，实得 {len(cols)}：{list(cols.keys())}"
    assert set(cols.keys()) == DOC_COLUMNS["orders"]


@pytest.mark.parametrize(
    "column",
    [
        "pick_code_enc",
        "auto_confirm_due_at",
        "pre_dispute_status",
        "version",
        "settle_state",
        "push_round",
        "last_push_at",
        "delivered_at",
        "accepted_at",
        "completed_at",
    ],
)
def test_a2_orders_critical_columns_exist(column: str) -> None:
    """任务书点名的 orders 关键列一个都不能少。"""
    assert column in Base.metadata.tables["orders"].columns, f"orders 缺列 {column}"


def test_a2_orders_has_no_updated_at() -> None:
    """5.2 的 orders 只有 created_at；`updated_at` 会暗示整行可改，与 append-only 审计冲突。"""
    assert "updated_at" not in Base.metadata.tables["orders"].columns


def test_a2_order_progress_is_append_only() -> None:
    """order_progress 是 append-only 事实流：只有 created_at，不许有 updated_at。"""
    cols = Base.metadata.tables["order_progress"].columns
    assert "created_at" in cols
    assert "updated_at" not in cols, "append-only 事实流不该有 updated_at"


# ---------------------------------------------------------------------------
# A3. 索引清单逐条对账
# ---------------------------------------------------------------------------
def test_a3_index_inventory_matches_doc() -> None:
    """27 条索引，表→索引名→(唯一, 部分索引 WHERE) 与 5.3 逐条一致。"""
    expected_pairs = {
        (table, name, unique, where)
        for table, items in DOC_INDEXES.items()
        for name, (unique, where) in items.items()
    }
    assert len(expected_pairs) == 27, f"5.3 应共 27 条索引，我的手抄表有 {len(expected_pairs)} 条"

    actual_pairs = set()
    for table_name, table in Base.metadata.tables.items():
        for index in table.indexes:
            actual_pairs.add((table_name, index.name, bool(index.unique), _where_of(index)))

    missing = expected_pairs - actual_pairs
    extra = actual_pairs - expected_pairs
    assert not missing, f"5.3 要求但缺失的索引：{sorted(missing)}"
    assert not extra, f"多出来（5.3 没要求）的索引：{sorted(extra)}"


def test_a3_total_and_partial_counts() -> None:
    """总 27 条、部分索引 5 条 —— 与 `alembic upgrade head --sql` 产物独立得出的数字一致。"""
    total = sum(len(t.indexes) for t in Base.metadata.tables.values())
    partial = [
        (t.name, i.name)
        for t in Base.metadata.tables.values()
        for i in t.indexes
        if _where_of(i) is not None
    ]
    assert total == 27, f"索引总数应 27，实得 {total}"
    assert len(partial) == 5, f"部分索引应 5 条，实得 {len(partial)}：{partial}"
    assert sorted(name for _table, name in partial) == sorted([
        "ix_orders_status_deadline",
        "ix_orders_runner_id_completed_at",
        "ix_orders_auto_confirm_due_at",
        "ix_orders_accepted_at",
        "uq_order_cancel_requests_order_id_pending",
    ])
    assert sorted(t for t, _n in partial) == [
        "order_cancel_requests", "orders", "orders", "orders", "orders",
    ]


def test_a3_orders_has_eight_indexes() -> None:
    """5.3 的 orders 恰好 8 条索引。"""
    names = sorted(i.name for i in Base.metadata.tables["orders"].indexes)
    assert len(names) == 8, f"orders 应 8 条索引，实得 {len(names)}：{names}"
    assert names == sorted(DOC_INDEXES["orders"])


def test_a3_auto_confirm_scan_uses_due_at() -> None:
    """BR-21/D4：24h 扫描的索引必须是 `(auto_confirm_due_at) WHERE status='DELIVERED'`。"""
    index = next(
        i for i in Base.metadata.tables["orders"].indexes
        if i.name == "ix_orders_auto_confirm_due_at"
    )
    assert [c.name for c in index.columns] == ["auto_confirm_due_at"]
    assert _where_of(index) == "status = 'DELIVERED'"


# ---------------------------------------------------------------------------
# A4. 反向检查：ADR-02 明令禁止的索引/类型一个都不能有
# ---------------------------------------------------------------------------
def test_a4_no_geo_index_and_no_delivered_at_index() -> None:
    """不建 `orders(to_geohash)`、不建 GIST/PostGIS、不建 `(status, delivered_at)`。"""
    all_index_names = {i.name for t in Base.metadata.tables.values() for i in t.indexes}
    for forbidden in ("to_geohash", "gist", "geom"):
        assert not any(forbidden in n.lower() for n in all_index_names), forbidden

    for table in Base.metadata.tables.values():
        for index in table.indexes:
            cols = [c.name for c in index.columns if hasattr(c, "name")]
            if table.name == "orders":
                assert "delivered_at" not in cols, (
                    f"{index.name} 索引了 delivered_at —— 它回滚后不是到期依据，纯写放大"
                )
            assert "from_geohash" not in cols and "to_geohash" not in cols


# ---------------------------------------------------------------------------
# B6. 类型口径：JSONB 而非 JSON、金额 Numeric 而非 Float、时间必须 timezone=True
# ---------------------------------------------------------------------------
def test_b6_all_datetime_columns_are_timezone_aware() -> None:
    """所有时间列必须 `DateTime(timezone=True)`（K-04：DB 一律 timestamptz/UTC）。"""
    bad: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, DateTime) and not column.type.timezone:
                bad.append(f"{table.name}.{column.name}")
    assert not bad, f"这些时间列不是 timezone=True：{bad}"


def test_b6_json_columns_are_jsonb() -> None:
    """5.2 里所有 jsonb 列都必须用 PG 的 JSONB，不能用通用 JSON。"""
    json_cols = [
        f"{t.name}.{c.name}"
        for t in Base.metadata.tables.values()
        for c in t.columns
        if type(c.type).__name__ in ("JSON", "JSONB")
    ]
    assert json_cols, "居然一个 JSONB 列都没有？"
    for entry in json_cols:
        table, column = entry.split(".")
        col_type = Base.metadata.tables[table].columns[column].type
        assert isinstance(col_type, JSONB), f"{entry} 不是 JSONB 而是 {type(col_type).__name__}"
    assert sorted(json_cols) == sorted([
        "users.notify_settings",
        "orders.images",
        "order_progress.images",
        "reviews.tags",
        "disputes.evidence",
        "notifications.payload",
        "sys_configs.value",
        "admin_audit_logs.before",
        "admin_audit_logs.after",
    ])


def test_b6_money_and_coords_are_numeric_not_float() -> None:
    """金额/坐标一律 Numeric（Decimal），禁止 Float（0.5 分累加会出 99.49999）。"""
    from sqlalchemy import Float, Numeric

    numeric_expect = {
        "users.credit_score": (5, 2),
        "orders.reward": (6, 2),
        "orders.from_lat": (9, 6),
        "orders.from_lng": (9, 6),
        "orders.to_lat": (9, 6),
        "orders.to_lng": (9, 6),
        "pois.lat": (9, 6),
        "pois.lng": (9, 6),
        "credit_logs.delta": (5, 2),
        "credit_logs.score_after": (5, 2),
        "push_logs.score": (6, 4),
    }
    for path, (precision, scale) in numeric_expect.items():
        table, column = path.split(".")
        col_type = Base.metadata.tables[table].columns[column].type
        assert isinstance(col_type, Numeric), f"{path} 是 {type(col_type).__name__}，应为 Numeric"
        assert not isinstance(col_type, Float)
        assert (col_type.precision, col_type.scale) == (precision, scale), (
            f"{path} 精度应为 Numeric({precision},{scale})，实得 "
            f"Numeric({col_type.precision},{col_type.scale})"
        )


def test_b6_phone_and_pick_code_are_bytea() -> None:
    """`phone_enc` / `pick_code_enc` 必须是 bytea（AES-GCM 密文），不能是 text。"""
    from sqlalchemy.dialects.postgresql import BYTEA

    for path in ("users.phone_enc", "orders.pick_code_enc"):
        table, column = path.split(".")
        col_type = Base.metadata.tables[table].columns[column].type
        assert isinstance(col_type, BYTEA), f"{path} 应为 BYTEA，实得 {type(col_type).__name__}"


def test_b6_varchar_lengths_for_status_and_action() -> None:
    """`orders.status` / `order_progress.action` 的长度必须装得下所有枚举值。"""
    from app.models.order import PROGRESS_ACTIONS
    from app.modules.order.state_machine import Action, Status

    status_len = Base.metadata.tables["orders"].columns["status"].type.length
    action_len = Base.metadata.tables["order_progress"].columns["action"].type.length
    # 5.2 原文写的是 varchar(12)，但 `IN_PROGRESS`(11) 只剩余量 1；
    # team-lead 裁定把所有"枚举列余量吃紧"的列统一放宽到 varchar(16)，
    # 详见 tests/test_schema_widths.py 的 HEADROOM_BUDGET 快照。
    assert status_len == 16
    assert action_len == 24
    for status in Status:
        assert len(status.value) <= status_len, status
    for action in PROGRESS_ACTIONS:
        assert len(action) <= action_len, action
    assert {a.value for a in Action} == set(PROGRESS_ACTIONS)


def test_b6_enum_lists_are_consistent_between_model_and_state_machine() -> None:
    """DB 层 CHECK 的取值范围与状态机枚举必须逐字一致（否则真库会拦合法写入）。"""
    from app.models.order import (
        ACTOR_ROLES,
        CANCEL_REQUEST_STATUSES,
        ORDER_STATUSES,
        PROGRESS_ACTIONS,
        SETTLE_STATES,
    )
    from app.modules.order.state_machine import Action

    assert set(ORDER_STATUSES) == {s.value for s in __import__(
        "app.modules.order.state_machine", fromlist=["Status"]
    ).Status}
    assert set(PROGRESS_ACTIONS) == {a.value for a in Action}
    assert set(ACTOR_ROLES) == {"PUBLISHER", "RUNNER", "ADMIN", "SYSTEM"}
    assert set(SETTLE_STATES) == {"UNSETTLED", "SETTLED", "FROZEN"}
    assert set(CANCEL_REQUEST_STATUSES) == {"PENDING", "APPROVED", "REJECTED"}
