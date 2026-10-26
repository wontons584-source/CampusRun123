"""真库集成测试（`@pytest.mark.integration`，**只在设置了 `TEST_DATABASE_URL` 时运行**）。

## 为什么必须有这个文件

`tests/test_schema_widths.py` 是纯模型层的宽度自检，它守得住"模型装的装不下"，
但守不住两件事：
1. **模型 ↔ 真实数据库的结构漂移**（改了模型没改迁移、或迁移没跑）；
2. **数据库自己会不会拒**（CHECK 约束、GENERATED 列的表达式、DEFAULT 值）。

历史上栽的就是第 2 类：`orders.settle_state varchar(8)` 与默认值 `'UNSETTLED'`(9)
冲突，**任何 `insert into orders` 都失败** —— 而 524 个单测因为用内存替身，全绿。

## 跑法

```bash
export TEST_DATABASE_URL="postgresql+psycopg://campusrun:campusrun@127.0.0.1:5433/campusrun"
pytest tests -m integration
```

未设置 `TEST_DATABASE_URL`（或连不上）时**整体 skip**，绝不让默认套件变红 ——
"本机没有数据库"是常态，不该阻塞其他人跑单测。

## 数据卫生

每个用例在自己的事务里跑，结束时 `rollback`：**不写脏数据、不依赖清库、可并行重跑**。
故本文件不碰应用自己的 `SessionLocal`（那指向 dev 库），只建一条独立连接。
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from app.models import Base
from sqlalchemy import String
from sqlalchemy.pool import NullPool

TEST_DATABASE_URL: str = os.environ.get("TEST_DATABASE_URL", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not TEST_DATABASE_URL,
        reason="未设置 TEST_DATABASE_URL，跳过真库集成测试（默认套件不受影响）",
    ),
]


# ---------------------------------------------------------------------------
# 夹具：引擎 + 每用例回滚的事务
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def engine() -> Iterator[sa.Engine]:
    if not TEST_DATABASE_URL:
        pytest.skip("未设置 TEST_DATABASE_URL")
    eng = sa.create_engine(TEST_DATABASE_URL, poolclass=NullPool, future=True)
    try:
        with eng.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - 连不上就 skip，不能红
        eng.dispose()
        pytest.skip(f"TEST_DATABASE_URL 不可达，跳过真库测试：{exc}")
    yield eng
    eng.dispose()


@pytest.fixture()
def conn(engine: sa.Engine) -> Iterator[sa.Connection]:
    """每用例一个事务，结束回滚（不落脏数据、不用清库）。"""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _seed_user(connection: sa.Connection, *, score: Any = 100, nickname: str = "测试用户") -> int:
    return int(
        connection.execute(
            sa.text(
                "INSERT INTO users (nickname, credit_score, verify_status, status) "
                "VALUES (:n, :s, 'APPROVED', 'ACTIVE') RETURNING id"
            ),
            {"n": nickname, "s": score},
        ).scalar_one()
    )


def _seed_poi(connection: sa.Connection, *, name: str = "驿站A") -> int:
    return int(
        connection.execute(
            sa.text(
                "INSERT INTO pois (campus_id, name, type, lat, lng) "
                "VALUES (1, :name, 'STATION', 32.060000, 118.780000) RETURNING id"
            ),
            {"name": name},
        ).scalar_one()
    )


def _seed_order(connection: sa.Connection, *, order_no: str = "CR250601000001") -> tuple[int, int]:
    """插入最小合法订单，返回 `(order_id, publisher_id)`。

    `type` 刻意用 `'BUY'` 而不是 `'EXPRESS'`：`ck_orders_express_pick_code` 要求
    代取快递必须带取件码（FR-ORDER-10），而这条约束有自己的用例
    （`test_express_order_requires_pick_code`）—— 这里的最小行不该顺带触发别的规则。
    """
    publisher_id = _seed_user(connection, nickname=f"发单人-{order_no}")
    poi_id = _seed_poi(connection, name=f"驿站-{order_no}")
    order_id = int(
        connection.execute(
            sa.text(
                """
                INSERT INTO orders (
                    order_no, publisher_id, type, from_poi_id, to_poi_id,
                    from_lat, from_lng, to_lat, to_lng,
                    reward, expect_minutes, deadline, status
                ) VALUES (
                    :no, :pid, 'BUY', :poi, :poi,
                    32.06, 118.78, 32.07, 118.79,
                    5.50, 30, now() + interval '1 hour', 'PENDING'
                ) RETURNING id
                """
            ),
            {"no": order_no, "pid": publisher_id, "poi": poi_id},
        ).scalar_one()
    )
    return order_id, publisher_id


# ---------------------------------------------------------------------------
# ① 发单写不进去（settle_state 默认值超长）—— 真库炸过的第一条
# ---------------------------------------------------------------------------
def test_orders_insert_succeeds_and_defaults_settle_state(conn: sa.Connection) -> None:
    """最小合法 `insert into orders` 必须成功，且 `settle_state` 默认 `'UNSETTLED'`。

    这条曾经必失败：`varchar(8)` 装不下默认值 `'UNSETTLED'`(9)，
    也就是 T-13 发单根本写不进库。
    """
    publisher_id = _seed_user(conn)
    poi_id = _seed_poi(conn)
    row = conn.execute(
        sa.text(
            """
            INSERT INTO orders (
                order_no, publisher_id, type, from_poi_id, to_poi_id,
                from_lat, from_lng, to_lat, to_lng,
                reward, expect_minutes, deadline, status
            ) VALUES (
                'CR250601000001', :pid, 'BUY', :poi, :poi,
                32.06, 118.78, 32.07, 118.79,
                5.50, 30, now() + interval '1 hour', 'PENDING'
            ) RETURNING id, settle_state, push_round, version, is_late, auto_confirmed
            """
        ),
        {"pid": publisher_id, "poi": poi_id},
    ).mappings().one()

    assert row["settle_state"] == "UNSETTLED"       # 默认值真的落进去了
    assert row["push_round"] == 0
    assert row["version"] == 0
    assert row["is_late"] is False
    assert row["auto_confirmed"] is False


def test_express_order_requires_pick_code(conn: sa.Connection) -> None:
    """FR-ORDER-10：`type='EXPRESS'` 不带给件码必须被 `ck_orders_express_pick_code` 拦。

    这条约束在修列宽的过程中暴露出来（先前的 `settle_state` 超长把错误挡在了它前面）：
    说明"修好一个约束才会看见下一个" —— 这正是真库测试不可替代的原因。
    """
    publisher_id = _seed_user(conn)
    poi_id = _seed_poi(conn)
    base_sql = """
        INSERT INTO orders (order_no, publisher_id, type, from_poi_id, to_poi_id,
            from_lat, from_lng, to_lat, to_lng, reward, expect_minutes, deadline,
            status, pick_code_enc)
        VALUES (:no, :pid, 'EXPRESS', :poi, :poi, 32.06, 118.78, 32.07, 118.79,
            5.50, 30, now() + interval '1 hour', 'PENDING', :pick)
    """
    conn.execute(sa.text("SAVEPOINT sp_express"))
    with pytest.raises(sa.exc.IntegrityError) as exc_info:
        conn.execute(sa.text(base_sql), {"no": "CREXP1", "pid": publisher_id, "poi": poi_id, "pick": None})
    conn.execute(sa.text("ROLLBACK TO SAVEPOINT sp_express"))
    assert "ck_orders_express_pick_code" in str(exc_info.value)

    # 带上密文（bytea）就通过
    row = conn.execute(
        sa.text(base_sql + " RETURNING id, length(pick_code_enc) AS n"),
        {"no": "CREXP2", "pid": publisher_id, "poi": poi_id, "pick": b"\x00ciphertext"},
    ).mappings().one()
    assert row["n"] == 11


def test_settle_state_accepts_all_three_values(conn: sa.Connection) -> None:
    """`settle_state` 的 3 个取值（最长 `'UNSETTLED'`=9）都必须写得进去。"""
    from app.models.order import SETTLE_STATES

    order_id, _ = _seed_order(conn)
    for state in SETTLE_STATES:
        conn.execute(
            sa.text("UPDATE orders SET settle_state = :s WHERE id = :o"),
            {"s": state, "o": order_id},
        )
    values = conn.execute(
        sa.text("SELECT DISTINCT settle_state FROM orders WHERE id = :o"), {"o": order_id}
    ).scalars().all()
    assert values == [SETTLE_STATES[-1]]  # 最后一轮写的值


def test_orders_reward_check_is_enforced(conn: sa.Connection) -> None:
    """BR-07：酬劳 2..50，越界必须被 `ck_orders_reward_range` 拦。"""
    publisher_id = _seed_user(conn)
    poi_id = _seed_poi(conn)
    for index, (reward, should_pass) in enumerate(
        (("2.00", True), ("50.00", True), ("1.90", False), ("50.50", False))
    ):
        savepoint = f"sp_reward_{index}"
        conn.execute(sa.text(f"SAVEPOINT {savepoint}"))
        try:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO orders (order_no, publisher_id, type, from_poi_id, to_poi_id,
                        from_lat, from_lng, to_lat, to_lng, reward, expect_minutes, deadline, status)
                    VALUES (:no, :pid, 'BUY', :poi, :poi, 32.06, 118.78, 32.07, 118.79,
                        :r, 30, now() + interval '1 hour', 'PENDING')
                    """
                ),
                {"no": f"CRRW{index}", "pid": publisher_id, "poi": poi_id, "r": reward},
            )
        except sa.exc.IntegrityError as exc:
            conn.execute(sa.text(f"ROLLBACK TO SAVEPOINT {savepoint}"))
            assert should_pass is False, f"reward={reward} 本应通过却被拒：{exc}"
            assert "ck_orders_reward_range" in str(exc)
        else:
            conn.execute(sa.text(f"RELEASE SAVEPOINT {savepoint}"))
            assert should_pass is True, f"reward={reward} 本应被拒却插入成功"


# ---------------------------------------------------------------------------
# ② 发单人的进度事件写不进去（actor_role='PUBLISHER' 超长）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["PUBLISHER", "RUNNER", "ADMIN", "SYSTEM"])
def test_order_progress_accepts_every_actor_role(conn: sa.Connection, role: str) -> None:
    """4 种 `actor_role` 都必须能写入。`'PUBLISHER'`(9) 曾经装不进 `varchar(8)`。"""
    order_id, publisher_id = _seed_order(conn)
    actor_id = None if role == "SYSTEM" else publisher_id
    row = conn.execute(
        sa.text(
            """
            INSERT INTO order_progress
                (order_id, seq, action, from_status, to_status, actor_id, actor_role, note)
            VALUES (:oid, 1, 'CREATE', 'PENDING', 'PENDING', :aid, :role, '初始事件')
            RETURNING actor_role, action
            """
        ),
        {"oid": order_id, "aid": actor_id, "role": role},
    ).mappings().one()
    assert row["actor_role"] == role


def test_order_progress_rejects_bogus_action(conn: sa.Connection) -> None:
    """`order_progress.action='BOGUS'` 必须被 CHECK 拦（枚举列不能靠应用层自觉）。"""
    order_id, publisher_id = _seed_order(conn)
    with pytest.raises(sa.exc.IntegrityError) as exc_info:
        conn.execute(
            sa.text(
                """
                INSERT INTO order_progress
                    (order_id, seq, action, from_status, to_status, actor_id, actor_role)
                VALUES (:oid, 1, 'BOGUS', 'PENDING', 'PENDING', :aid, 'PUBLISHER')
                """
            ),
            {"oid": order_id, "aid": publisher_id},
        )
    assert "action" in str(exc_info.value)


def test_order_progress_accepts_every_action(conn: sa.Connection) -> None:
    """19 个 action 全部能写进去（枚举值最长 `ARBITRATE_LIABLE`=16 ≤ varchar(20)）。"""
    from app.modules.order.state_machine import Action

    order_id, publisher_id = _seed_order(conn)
    for seq, action in enumerate(Action, start=1):
        conn.execute(
            sa.text(
                """
                INSERT INTO order_progress
                    (order_id, seq, action, from_status, to_status, actor_id, actor_role)
                VALUES (:oid, :seq, :action, 'IN_PROGRESS', 'IN_PROGRESS', :aid, 'RUNNER')
                """
            ),
            {"oid": order_id, "seq": seq, "action": action.value, "aid": publisher_id},
        )
    total = conn.execute(
        sa.text("SELECT count(*) FROM order_progress WHERE order_id = :oid"), {"oid": order_id}
    ).scalar_one()
    assert total == len(Action) == 19


# ---------------------------------------------------------------------------
# ③ credit_level 生成列：分数一插入就炸，且四档必须算对
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "score,level",
    [
        ("115", "EXCELLENT"),
        ("110", "EXCELLENT"),   # BR-26 边界：≥110 优秀
        ("109.99", "GOOD"),
        ("100", "GOOD"),        # 默认分 → 良好
        ("95", "GOOD"),         # 边界：95–109 良好
        ("85", "NORMAL"),       # 80–94 正常
        ("80", "NORMAL"),       # 边界
        ("79.99", "LIMITED"),
        ("70", "LIMITED"),      # <80 受限
        ("0", "LIMITED"),       # BR-25 下界
    ],
)
def test_credit_level_generated_column(conn: sa.Connection, score: str, level: str) -> None:
    """`users.credit_level` 是 GENERATED 列，四个分档必须与 BR-26 一致。

    `credit_score=115` 曾经直接 `ERROR: value too long for type character varying(8)`。
    """
    row = conn.execute(
        sa.text(
            "INSERT INTO users (nickname, credit_score, verify_status, status) "
            "VALUES (:n, :s, 'APPROVED', 'ACTIVE') RETURNING credit_score, credit_level"
        ),
        {"n": f"等级测试{score}", "s": score},
    ).mappings().one()
    assert row["credit_level"] == level, f"score={score} 应为 {level}，实得 {row['credit_level']}"


def test_credit_level_is_not_writable(conn: sa.Connection) -> None:
    """生成列禁止应用层写入（防"分数改了等级没改"的脏数据）。"""
    conn.execute(sa.text("SAVEPOINT sp_width"))
    with pytest.raises(sa.exc.DBAPIError) as exc_info:
        conn.execute(
            sa.text("INSERT INTO users (nickname, credit_score, credit_level) VALUES ('x', 100, 'GOOD')")
        )
    conn.execute(sa.text("ROLLBACK TO SAVEPOINT sp_width"))
    assert "GENERATED" in str(exc_info.value) or "generated" in str(exc_info.value).lower()


@pytest.mark.parametrize("score", ["200", "120.01", "-1"])
def test_credit_score_out_of_range_rejected(conn: sa.Connection, score: str) -> None:
    """BR-25：0–120，越界必须被 `ck_users_credit_score_range` 拦。"""
    conn.execute(sa.text("SAVEPOINT sp_score"))
    with pytest.raises(sa.exc.IntegrityError) as exc_info:
        conn.execute(
            sa.text("INSERT INTO users (nickname, credit_score) VALUES ('越界', :s)"),
            {"s": score},
        )
    conn.execute(sa.text("ROLLBACK TO SAVEPOINT sp_score"))
    assert "credit_score" in str(exc_info.value)


# ---------------------------------------------------------------------------
# ④ 全库扫描：任何 varchar 列都不允许比它的 CHECK 允许值更窄
# ---------------------------------------------------------------------------
_WIDTH_SQL = """
SELECT c.relname AS tbl, a.attname AS col, a.atttypmod - 4 AS width
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind = 'r'
  AND a.attnum > 0
  AND NOT a.attisdropped
  AND a.atttypid = 'character varying'::regtype
  AND a.atttypmod > 4
"""

_CHECK_SQL = """
SELECT conrelid::regclass::text AS tbl, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE contype = 'c'
  AND connamespace = 'public'::regnamespace
"""


def test_no_varchar_wider_gap_between_check_values_and_width(conn: sa.Connection) -> None:
    """**扫描全库**：每个 varchar 列，凡是 CHECK 里列举了允许值的，最长值必须装得下。

    这就是"三个致命缺陷"的一般化检测：不针对某张表某列，而是把
    「DB 说这列只能取这些值」与「DB 说这列最多 n 个字符」放在一起比。
    `credit_level` 那种**没有 CHECK 的生成列**由 `test_generated_column_widths`
    与纯模型层的 `test_schema_widths.py` 兜（两边一起才闭环）。

    注意 PG 会把 `col IN ('A','B')` 重写成
    `((col)::text = ANY ((ARRAY['A'::character varying, ...])::text[]))`，
    所以解析的是 `= ANY (ARRAY[...])` 形式，不是原始 SQL 文本。
    """
    widths: dict[tuple[str, str], int] = {
        (str(r.tbl), str(r.col)): int(r.width)
        for r in conn.execute(sa.text(_WIDTH_SQL)).all()
    }
    assert len(widths) > 30, f"扫描到的 varchar 列只有 {len(widths)} 个，SQL 可能写错了"

    # `((col)::text = ANY ((ARRAY['A'::character varying, 'B'::character varying])::text[]))`
    # 也覆盖 `col IS NULL OR col = ANY (...)`（定义里是 `((col IS NULL) OR (...))`）
    pattern = re.compile(r"\((\w+)\)::text = ANY \(\(ARRAY\[(?P<vals>.*?)\]\)", re.S)

    checked = 0
    problems: list[str] = []
    for row in conn.execute(sa.text(_CHECK_SQL)).all():
        table = str(row.tbl).strip('"')
        if table == "alembic_version":
            continue
        matched = pattern.search(str(row.definition))
        if not matched:
            continue
        column = matched.group(1)
        values = re.findall(r"'([^']*)'", matched.group("vals"))
        width = widths.get((table, column))
        if width is None or not values:
            continue
        checked += 1
        longest = max(values, key=len)
        if len(longest) > width:
            problems.append(
                f"{table}.{column}: varchar({width}) 装不下 CHECK 允许值 {longest!r}({len(longest)})"
            )

    assert checked >= 18, f"只解析到 {checked} 个带 IN-CHECK 的 varchar 列，checklist 不完整"
    assert not problems, "真库列宽不足：\n  " + "\n  ".join(problems)


def test_generated_column_widths(conn: sa.Connection) -> None:
    """生成列没有 CHECK 可扫，单独验证：最长分档值必须装得下。"""
    from app.models.user import CREDIT_LEVEL_EXPR

    levels = re.findall(r"'([A-Z_]+)'", CREDIT_LEVEL_EXPR)
    width = conn.execute(
        sa.text(
            "SELECT a.atttypmod - 4 FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE c.relname = 'users' AND a.attname = 'credit_level'"
        )
    ).scalar_one()
    assert width >= max(len(x) for x in levels), (
        f"users.credit_level varchar({width}) 装不下 {max(levels, key=len)!r}"
    )


# ---------------------------------------------------------------------------
# ⑤ 模型 ↔ 真库结构漂移
# ---------------------------------------------------------------------------
def test_live_schema_matches_model_metadata(conn: sa.Connection) -> None:
    """14 张表都在，且**所有 varchar 列的宽度与模型声明一致**。

    这条能抓住"改了模型没跑迁移"：模型已是 varchar(16)、库里还是 varchar(8) 时立刻红。
    """
    inspector = sa.inspect(conn)
    live_tables = set(inspector.get_table_names()) - {"alembic_version"}
    model_tables = set(Base.metadata.tables)
    assert live_tables == model_tables, (
        f"表不一致：库多={sorted(live_tables - model_tables)} 库少={sorted(model_tables - live_tables)}"
    )

    drift: list[str] = []
    for table in Base.metadata.sorted_tables:
        live_columns = {c["name"]: c for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if not isinstance(column.type, String):
                continue
            live = live_columns.get(column.name)
            if live is None:
                drift.append(f"{table.name}.{column.name}: 库里缺这一列")
                continue
            live_width = getattr(live["type"], "length", None)
            if live_width != column.type.length:
                drift.append(
                    f"{table.name}.{column.name}: 模型 varchar({column.type.length}) ≠ "
                    f"库 varchar({live_width})"
                )
    assert not drift, "模型与真库结构漂移（迁移没跑？）：\n  " + "\n  ".join(drift)


def test_migration_is_at_head(conn: sa.Connection) -> None:
    """库里必须已经在 head 修订上（否则跑的是旧 schema）。

    注意：head 前移（如新增迁移）时这里要同步改。可以用 alembic 的 script 目录
    动态取 head，但那样会引入 alembic runtime 依赖；显式断言当前 head 更直接，
    漏改会在这里红，提醒迁移测试也要跟着走。
    """
    revision = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    assert revision == "0002", f"当前修订 {revision}，期望 0002（head）"


def test_generated_column_and_partial_indexes_exist_in_live_db(conn: sa.Connection) -> None:
    """两个"只有真库能验证"的结构：GENERATED 列与 5 条部分索引。"""
    generated = conn.execute(
        sa.text(
            "SELECT is_generated, generation_expression FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'credit_level'"
        )
    ).mappings().one()
    assert generated["is_generated"] == "ALWAYS"
    assert "credit_score" in (generated["generation_expression"] or "")

    partial = conn.execute(
        sa.text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'public' AND indexdef ILIKE '%WHERE%'"
        )
    ).scalars().all()
    expected = {
        "ix_orders_status_deadline",
        "ix_orders_runner_id_completed_at",
        "ix_orders_auto_confirm_due_at",
        "ix_orders_accepted_at",
        "uq_order_cancel_requests_order_id_pending",
    }
    assert expected <= set(partial), f"缺失部分索引：{sorted(expected - set(partial))}"
