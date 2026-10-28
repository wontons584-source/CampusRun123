"""订单仓储：状态机协议的真实 SQLAlchemy 实现（T-05）。

## 这个文件存在的理由

在此之前，状态机的全部测试都跑在 `tests/conftest.py::MemoryOrderRepo` 上。
它是**内存替身**，有 7 处与真库语义不同 —— 每一处都是"单测全绿、上线才炸"的坑：

| # | 内存替身的行为 | 本实现 |
|---|---|---|
| 1 | `setdefault` 凭空建行，未 seed 也能 CAS 成功 | `UPDATE ... WHERE id=? AND status=?`，0 行返回 0（由 `apply()` 抛 40905） |
| 2 | `seq` 进程内全局自增 | `COALESCE(MAX(seq),0)+1 WHERE order_id=?`，**每单从 1 开始** |
| 3 | 不校验 `UNIQUE(order_id, seq)` | 撞唯一约束 → **重算 seq 重试（最多 3 次）**，仍失败抛业务错 |
| 4 | patch 写错键也"通过" | patch 键必须是 `orders` 的真实列，否则抛 `RepoProgrammingError`（编程错误，不是用户错误） |
| 5 | 无 `version` 乐观锁 | CAS 里 `version = version + 1` |
| 6 | 不模拟任何 CHECK | `IntegrityError` 按 **约束名** 映射成业务错误码，而不是一律 500 |
| 7 | CAS 失败不回滚事件 | 不 commit（K-09）；CAS 0 行时由调用方事务整体回滚，事件不落库 |

## 事务边界（K-09）

**本类永不 commit**。一个 HTTP 请求 = 一个事务，事务归 `api` 层；
Celery 任务自己开事务。仓储只在一个已经打开的事务里执行语句，
这样 `apply()` 的「先 append 事实、再 CAS 聚合」才是原子的：
CAS 返回 0 行 → `apply()` 抛 40905 → 调用方回滚 → 那条事件一起消失。

## 为什么不直接用 ORM 对象改状态

`session.get(Order, id)` + `order.status = ...` 是「读-改-写」，
并发下两个请求都能读到 `IN_PROGRESS` 再各自写成功 —— 这就是 CAS 要解决的问题。
`cas_update_status` 用一条带 `WHERE status=:expected` 的 UPDATE，
把「比较」和「交换」压到数据库的一行锁里。
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import clock as default_clock
from app.core.errors import ApiError, ErrCode
from app.models.order import Order
from app.modules.order.state_machine import (
    NODE_ORDER,
    RUNNER_EVIDENCE,
    Action,
    OrderCtx,
    ProgressEvent,
    Role,
    Status,
)

#: `order_progress` 表列集合（patch 键校验用），模块加载时算一次。
_ORDER_COLUMNS: Final[frozenset[str]] = frozenset(
    column.name for column in Order.__table__.columns
)

#: 撞 `UNIQUE(order_id, seq)` 时的最大重试次数（并发写同一单的进度事件）。
_SEQ_RETRY_LIMIT: Final[int] = 3

# 把常量拼进 SQL 字面量：RUNNER_EVIDENCE / NODE_ORDER 是**唯一**口径定义点，
# 这里只做拼接、不重写取值，避免"仓储与状态机各有一套白名单"。
_EVIDENCE_LITERAL: Final[str] = ", ".join(f"'{value}'" for value in RUNNER_EVIDENCE)
_NODE_LITERAL: Final[str] = ", ".join(f"'{value}'" for value in NODE_ORDER)


class RepoProgrammingError(RuntimeError):
    """仓储使用方的**编程错误**（不是用户错误）。

    典型场景：`status_patch()` 产出的键不在 `orders` 表里（改了状态机却忘了加列）。
    这类错误绝不能映射成 4xx —— 那不是用户能修的；也不该被 except 吞掉，
    应该 500 + 完整堆栈进日志，让它在测试阶段就炸出来。
    """


@dataclass(frozen=True)
class OrderDraft:
    """发单入参（T1 的落库载荷）。

    `pick_code_enc` 必须是**密文**：明文不许进仓储层（K-08）。
    `type='EXPRESS'` 时非空 —— 由 `ck_orders_express_pick_code` 兜底，
    但 T-13 必须在 service 层先校验并抛 40002，不能让 DB 抛 500。
    """

    order_no: str
    publisher_id: int
    type: str
    from_poi_id: int
    to_poi_id: int
    from_lat: Decimal
    from_lng: Decimal
    to_lat: Decimal
    to_lng: Decimal
    reward: Decimal
    expect_minutes: int
    deadline: datetime.datetime
    description: str | None = None
    images: tuple[str, ...] = ()
    pick_code_enc: bytes | None = None


# ---------------------------------------------------------------------------
# IntegrityError → 业务错误码
# ---------------------------------------------------------------------------
#: 唯一约束名 → (HTTP, 业务码, 文案)。按**约束名**而不是"有没有 23505"来映射，
#: 因为同样是唯一冲突，"订单号撞了"要提示重试、"重复评价"要提示已评过。
_UNIQUE_CONSTRAINT_MAP: Final[dict[str, tuple[int, int, str]]] = {
    "uq_orders_order_no": (409, ErrCode.ORDER_NO_CONFLICT, "订单号重复，请重试"),
    "uq_order_progress_order_id_seq": (409, ErrCode.STATUS_CHANGED_CONCURRENTLY, "进度事件序号冲突"),
    "uq_reviews_order_id_from_id": (409, ErrCode.REVIEW_ALREADY_EXISTS, "该订单你已评价过"),
    "uq_disputes_order_id": (409, ErrCode.DISPUTE_ALREADY_EXISTS, "该订单已有纠纷记录"),
    "uq_order_cancel_requests_order_id_pending": (
        409,
        ErrCode.CANCEL_REQUEST_EXISTS,
        "已有待响应的取消申请",
    ),
    "uq_auth_identities_provider_provider_uid": (409, ErrCode.RESOURCE_CONFLICT, "该登录方式已绑定"),
    "uq_users_student_no": (409, ErrCode.RESOURCE_CONFLICT, "该学号已被占用"),
    "uq_pois_campus_id_name": (409, ErrCode.RESOURCE_CONFLICT, "该校区已有同名地点"),
}

#: CHECK 约束名 → (HTTP, 业务码, 文案)。**不允许**落到 500：
#: 这些都是"入参不合规"，用户能改。
_CHECK_CONSTRAINT_MAP: Final[dict[str, tuple[int, int, str]]] = {
    "ck_order_progress_action_enum": (422, ErrCode.ILLEGAL_TRANSITION, "非法的进度动作"),
    "ck_orders_reward_range": (400, ErrCode.REWARD_OUT_OF_RANGE, "酬劳需在 2–50 元之间"),
    "ck_orders_express_pick_code": (
        400,
        ErrCode.PICK_CODE_REQUIRED,
        "代取快递必须提供取件码",
    ),
    "ck_orders_type_enum": (400, ErrCode.INVALID_ARGUMENT, "不支持的任务类型"),
    "ck_orders_status_enum": (422, ErrCode.ILLEGAL_TRANSITION, "非法的订单状态"),
    "ck_orders_settle_state_enum": (422, ErrCode.INVALID_ARGUMENT, "非法的结算状态"),
    "ck_users_credit_score_range": (400, ErrCode.INVALID_ARGUMENT, "信誉分需在 0–120 之间"),
    "ck_reviews_rating_range": (400, ErrCode.INVALID_ARGUMENT, "评分需在 1–5 之间"),
    "ck_order_progress_actor_role_enum": (400, ErrCode.INVALID_ARGUMENT, "非法的操作者角色"),
    "ck_order_cancel_requests_status_enum": (422, ErrCode.INVALID_ARGUMENT, "非法的取消申请状态"),
}

#: FK 约束名 → (HTTP, 业务码, 文案)。
#: `order_progress.order_id` 指向一个不存在的订单时，真库在 append 阶段就抛 FK ——
#: 这比"CAS 0 行"更早、也更准确，所以映射成 404 而不是 400（"订单不存在"不是参数错）。
_FK_CONSTRAINT_MAP: Final[dict[str, tuple[int, int, str]]] = {
    "fk_order_progress_order_id_orders": (404, ErrCode.ORDER_NOT_FOUND, "订单不存在"),
    "fk_order_cancel_requests_order_id_orders": (404, ErrCode.ORDER_NOT_FOUND, "订单不存在"),
    "fk_disputes_order_id_orders": (404, ErrCode.ORDER_NOT_FOUND, "订单不存在"),
    "fk_orders_publisher_id_users": (404, ErrCode.USER_NOT_FOUND, "发单人不存在"),
    "fk_orders_runner_id_users": (404, ErrCode.USER_NOT_FOUND, "接单人不存在"),
    "fk_orders_from_poi_id_pois": (404, ErrCode.POI_NOT_FOUND, "起点不存在"),
    "fk_orders_to_poi_id_pois": (404, ErrCode.POI_NOT_FOUND, "终点不存在"),
}

#: 服务端（PostgreSQL）错误码
_PG_UNIQUE_VIOLATION: Final[str] = "23505"
_PG_CHECK_VIOLATION: Final[str] = "23514"
_PG_FK_VIOLATION: Final[str] = "23503"
_PG_NOT_NULL_VIOLATION: Final[str] = "23502"


def _constraint_name_of(exc: DBAPIError) -> str:
    """从 psycopg 异常里抠约束名（优先 `diag.constraint_name`，其次 message）。"""
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    if name:
        return str(name)
    matched = re.search(r'constraint "([^"]+)"', str(orig or exc))
    return matched.group(1) if matched else ""


def _pg_code_of(exc: DBAPIError) -> str:
    """PostgreSQL SQLSTATE（`23505` 唯一冲突 / `23514` CHECK / `23503` FK / `23502` NOT NULL）。"""
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "pgcode", None)
    if code:
        return str(code)
    diag = getattr(orig, "diag", None)
    return str(getattr(diag, "sqlstate", "") or "")


def map_integrity_error(exc: IntegrityError, *, default_message: str = "数据校验未通过") -> ApiError:
    """把 `IntegrityError` 翻译成**业务错误**（这是第 6 条语义差异的落点）。

    顺序：约束名精确匹配 → 按 SQLSTATE 分类 → 都不是则原样抛出（不吞）。
    """
    constraint = _constraint_name_of(exc)
    if constraint in _UNIQUE_CONSTRAINT_MAP:
        http, code, message = _UNIQUE_CONSTRAINT_MAP[constraint]
        return ApiError(http, code, message)
    if constraint in _CHECK_CONSTRAINT_MAP:
        http, code, message = _CHECK_CONSTRAINT_MAP[constraint]
        return ApiError(http, code, message)
    if constraint in _FK_CONSTRAINT_MAP:
        http, code, message = _FK_CONSTRAINT_MAP[constraint]
        return ApiError(http, code, message)

    sqlstate = _pg_code_of(exc)
    if sqlstate == _PG_UNIQUE_VIOLATION:
        return ApiError(409, ErrCode.RESOURCE_CONFLICT, default_message)
    if sqlstate == _PG_CHECK_VIOLATION:
        return ApiError(422, ErrCode.INVALID_ARGUMENT, default_message)
    if sqlstate == _PG_FK_VIOLATION:
        return ApiError(400, ErrCode.INVALID_ARGUMENT, "关联的对象不存在")
    if sqlstate == _PG_NOT_NULL_VIOLATION:
        return ApiError(400, ErrCode.INVALID_ARGUMENT, "缺少必填字段")
    raise exc  # 认不出来就原样抛，绝不假装是业务错误


# ---------------------------------------------------------------------------
# 仓储
# ---------------------------------------------------------------------------
#: `load_ctx` 的**单条**查询：订单行 + 7 个派生事实。
#: 为什么压成一条：T2 抢单/T16 投诉都是用户点按钮的热路径，
#: 一条 SQL 一次往返，比"查订单再查 6 次聚合"少 6 个 RTT（N+1 的直接解法）。
#:
#: ## `runner_inflight_count` 的口径坑（真库 + 并发用例挖出来的 P0）
#:
#: BR-16「在途 ≤3」问的是「**谁要来抢单**，他手上几单」，而不是「本单的接单人手上几单」。
#: 抢单发生在 `status='PENDING'`、`runner_id IS NULL` 的时刻 —— 初版写成
#: `WHERE o.runner_id IS NOT NULL AND x.runner_id = o.runner_id` 时，
#: 第一个条件恒假，计数永远是 0，于是 `guard_grab` 的 `inflight < 3` **永远放行**：
#: 手上已有 3 单在途的接单人还能继续抢第 4 单（engineer-2 的集成用例实际撞到了）。
#:
#: 修法：`viewer_id` 由调用方传入（抢单/接单类操作传 `actor.id`），
#: 用 `COALESCE(:viewer_id, o.runner_id)` 回落到本单接单人 —— 不传时行为与旧版一致。
#: 显式 `CAST(:viewer_id AS bigint)` 是必需的：PG 对全 NULL 参数无法推断类型，
#: 直接 `COALESCE(:viewer_id, o.runner_id)` 会报 "could not determine data type"。
_LOAD_CTX_SQL: Final[str] = f"""
SELECT
    o.id, o.status, o.type, o.publisher_id, o.runner_id,
    o.accepted_at, o.delivered_at, o.completed_at,
    o.deadline, o.auto_confirm_due_at, o.pre_dispute_status, o.version,
    (SELECT count(*) FROM disputes d
        WHERE d.order_id = o.id) AS dispute_count,
    (SELECT count(*) FROM order_progress p
        WHERE p.order_id = o.id AND p.action = 'URGE') AS urge_count,
    (SELECT r.status FROM order_cancel_requests r
        WHERE r.order_id = o.id ORDER BY r.id DESC LIMIT 1) AS cancel_request_status,
    (SELECT p.created_at FROM order_progress p
        WHERE p.order_id = o.id
          AND p.actor_id = o.runner_id
          AND p.action IN ({_EVIDENCE_LITERAL})
        ORDER BY p.seq DESC LIMIT 1) AS last_runner_evidence_at,
    (SELECT p.action FROM order_progress p
        WHERE p.order_id = o.id
          AND p.actor_id = o.runner_id
          AND p.action IN ({_NODE_LITERAL})
        ORDER BY p.seq DESC LIMIT 1) AS last_node,
    (SELECT count(*) FROM orders x
        WHERE x.runner_id = COALESCE(CAST(:viewer_id AS bigint), o.runner_id)
          AND x.status = 'IN_PROGRESS') AS runner_inflight_count,
    (SELECT count(*) FROM orders y
        WHERE y.publisher_id = o.publisher_id
          AND y.status = 'PENDING') AS publisher_pending_count
FROM orders o
WHERE o.id = :order_id
"""

#: 分配 `seq` 并插入事件。
#: 聚合无 GROUP BY 时对空集也返回一行（MAX 为 NULL），所以「本单第一条事件」得 1。
_APPEND_PROGRESS_SQL: Final[str] = """
INSERT INTO order_progress
    (order_id, seq, action, from_status, to_status, actor_id, actor_role, note, images, created_at)
SELECT
    :order_id,
    COALESCE(MAX(seq), 0) + 1,
    :action, :from_status, :to_status, :actor_id, :actor_role, :note,
    CAST(:images AS jsonb), :created_at
FROM order_progress
WHERE order_id = :order_id
RETURNING seq
"""

#: 追加事件前先锁住**聚合根这一行**。
#:
#: 为什么必须有它：`MAX(seq)+1` 是「读-算-写」，20 个并发请求会同时读到同一个
#: `MAX(seq)` 然后算出同一个 `seq`，`UNIQUE(order_id, seq)` 只放行一个 ——
#: 实测 20 线程并发时有 19 个双双撞车，光靠"失败重试"根本收敛不了（重试也是同时发生，
#: 又一起撞）。行锁把同一单的追加串行化，seq 天然不冲突；重试退化为极端情况下的兜底。
#:
#: 用 `orders` 行锁而不是 advisory lock：语义就是「写这个聚合要先拿聚合根」，
#: 且它随后本来就要被 CAS `UPDATE` 锁一次，不引入新的锁对象/锁顺序。
#: 代价是 append 多一次往返（单张订单生命周期内 append 次数 ≤ 数十），完全可接受。
_LOCK_ORDER_SQL: Final[str] = "SELECT id FROM orders WHERE id = :order_id FOR UPDATE"


class SqlOrderRepo:
    """`state_machine.OrderRepo` 协议的真实实现。

    构造即绑定一个 `Session`（不 commit、不开事务）；同一个 session 里
    `append_progress` 与 `cas_update_status` 天然同事务。
    """

    __slots__ = ("_db", "_clock")

    def __init__(self, db: Session, *, clock: Any = None) -> None:
        self._db = db
        self._clock = clock or default_clock

    # ------------------------------------------------------------------ 读
    def load_ctx(self, order_id: int, *, viewer_id: int | None = None) -> OrderCtx:
        """一次查询拿全 `OrderCtx`（含 T16/T17 需要的进度事实）。

        抛出 `404/40401` 而不是 `None`：调用方拿到 `None` 之后大概率会
        `ctx.status` 空指针，不如在边界上就把"订单不存在"变成一个明确的业务错误。

        Args:
            order_id: 目标订单。
            viewer_id: **谁在操作**。只影响 `runner_inflight_count`（BR-16 在途 ≤3）：
                抢单/接单类操作必须传 `actor.id` —— 因为抢单那一刻本单的
                `runner_id` 还是 NULL，按本单接单人算永远是 0，守卫会形同虚设。
                不传时回落到 `orders.runner_id`（"查这一单时看它自己的接单人"），
                与旧行为完全一致，老调用点零影响。
        """
        row = (
            self._db.execute(
                sa.text(_LOAD_CTX_SQL),
                {"order_id": order_id, "viewer_id": viewer_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise ApiError(404, ErrCode.ORDER_NOT_FOUND, "订单不存在")

        last_evidence_at = row["last_runner_evidence_at"]
        return OrderCtx(
            id=int(row["id"]),
            status=str(row["status"]),
            publisher_id=int(row["publisher_id"]),
            runner_id=int(row["runner_id"]) if row["runner_id"] is not None else None,
            accepted_at=row["accepted_at"],
            delivered_at=row["delivered_at"],
            # has_runner_evidence 与 silent_since 同源：有最后一条证据时刻就说明有过进度
            has_runner_evidence=last_evidence_at is not None,
            last_runner_evidence_at=last_evidence_at,
            pre_dispute_status=row["pre_dispute_status"],
            deadline=row["deadline"],
            completed_at=row["completed_at"],
            auto_confirm_due_at=row["auto_confirm_due_at"],
            last_node=row["last_node"],
            dispute_count=int(row["dispute_count"] or 0),
            cancel_request_status=row["cancel_request_status"],
            runner_inflight_count=int(row["runner_inflight_count"] or 0),
            publisher_pending_count=int(row["publisher_pending_count"] or 0),
            urge_count=int(row["urge_count"] or 0),
            order_type=str(row["type"]),
        )

    def load_ctx_if_exists(
        self, order_id: int, *, viewer_id: int | None = None
    ) -> OrderCtx | None:
        """`load_ctx` 的容错版：不存在返回 `None`（供后台批量对账用）。"""
        try:
            return self.load_ctx(order_id, viewer_id=viewer_id)
        except ApiError as exc:
            if exc.code == ErrCode.ORDER_NOT_FOUND:
                return None
            raise

    # ------------------------------------------------------------- 写事实
    def append_progress(self, event: ProgressEvent, *, created_at: datetime.datetime) -> int:
        """追加一条 append-only 事实，返回分配到的 `seq`（每单从 1 开始）。

        两步：① `SELECT ... FOR UPDATE` 锁住订单行（串行化同一单的并发追加）；
        ② `INSERT ... SELECT MAX(seq)+1`（此时不可能撞号）。

        留着重试是因为**还有别的写入方**（后台脚本、数据修复）可能不带这把锁；
        真撞上了就重算重试，用 SAVEPOINT 隔离 —— 否则一次唯一冲突会把外层事务
        打成 aborted，整个请求报废。
        """
        self._lock_order(event.order_id)

        params = {
            "order_id": event.order_id,
            "action": event.action.value if isinstance(event.action, Action) else str(event.action),
            "from_status": event.from_status,
            "to_status": event.to_status,
            "actor_id": event.actor_id,
            "actor_role": event.actor_role.value
            if isinstance(event.actor_role, Role)
            else str(event.actor_role),
            "note": event.note,
            "images": json.dumps(list(event.images), ensure_ascii=False),
            "created_at": created_at,
        }

        last_error: IntegrityError | None = None
        for _attempt in range(_SEQ_RETRY_LIMIT):
            savepoint = self._db.begin_nested()
            try:
                seq = self._db.execute(sa.text(_APPEND_PROGRESS_SQL), params).scalar_one()
            except IntegrityError as exc:
                savepoint.rollback()
                last_error = exc
                if _constraint_name_of(exc) == "uq_order_progress_order_id_seq":
                    continue  # 并发撞号：重算 seq 再来一次
                raise map_integrity_error(exc, default_message="进度事件写入失败") from exc
            savepoint.commit()
            return int(seq)

        raise ApiError(
            409,
            ErrCode.STATUS_CHANGED_CONCURRENTLY,
            "订单进度正在被并发修改，请刷新后重试",
        ) from last_error

    def cas_update_status(
        self,
        *,
        order_id: int,
        expected_status: str,
        to_status: str,
        patch: Mapping[str, Any],
    ) -> int:
        """CAS：`UPDATE orders SET status=:to, version=version+1, **patch WHERE id=:id AND status=:expected`。

        返回受影响行数（1 成功 / 0 说明状态已被别人改掉，由 `apply()` 抛 40905）。
        **不会**因为 `order_id` 不存在就凭空建行 —— 那是内存替身的第 1 个坑。
        """
        self._assert_patch_keys(patch)
        values: dict[str, Any] = dict(patch)
        values["status"] = to_status
        values["version"] = Order.__table__.c.version + 1

        stmt = (
            sa.update(Order.__table__)
            .where(Order.__table__.c.id == order_id)
            .where(Order.__table__.c.status == expected_status)
            .values(**values)
        )
        try:
            result = self._db.execute(stmt)
        except IntegrityError as exc:
            raise map_integrity_error(exc, default_message="订单状态更新失败") from exc
        return int(result.rowcount or 0)

    # ------------------------------------------------------------ T1 建单
    def create_order(
        self,
        draft: OrderDraft,
        *,
        now: datetime.datetime | None = None,
    ) -> int:
        """T1：`orders` 与 `CREATE` 事件**同事务**写入，返回新订单 id。

        T1 不走 `apply()`（7.4：由 create() 直接落库），但**仍要写一条事实**，
        否则 `order_progress` 的时间轴会从"第二条"开始，投影对账时首条状态缺失。
        `CREATE` 事件的 `from_status` 填 `'PENDING'` = `to_status`：
        该列 NOT NULL，而"建单前"没有状态可用。
        """
        moment = self._clock.to_utc(now) if now is not None else self._clock.now()
        order_table = Order.__table__

        insert_order = (
            sa.insert(order_table)
            .values(
                order_no=draft.order_no,
                publisher_id=draft.publisher_id,
                runner_id=None,
                type=draft.type,
                from_poi_id=draft.from_poi_id,
                to_poi_id=draft.to_poi_id,
                from_lat=draft.from_lat,
                from_lng=draft.from_lng,
                to_lat=draft.to_lat,
                to_lng=draft.to_lng,
                reward=draft.reward,
                description=draft.description,
                # 传 list 而不是 json 字符串：SQLAlchemy 的 JSONB 类型会自己 json.dumps，
                # 传字符串会被二次编码成 JSON 字符串字面量（`"[...]"`）。
                images=list(draft.images),
                pick_code_enc=draft.pick_code_enc,
                expect_minutes=draft.expect_minutes,
                deadline=draft.deadline,
                status=Status.PENDING.value,
                version=0,
            )
            .returning(order_table.c.id)
        )
        try:
            order_id = int(self._db.execute(insert_order).scalar_one())
        except IntegrityError as exc:
            raise map_integrity_error(exc, default_message="订单写入失败") from exc

        self.append_progress(
            ProgressEvent(
                order_id=order_id,
                action=Action.CREATE,
                from_status=Status.PENDING.value,
                to_status=Status.PENDING.value,
                actor_id=draft.publisher_id,
                actor_role=Role.PUBLISHER,
                note=None,
                images=(),
            ),
            created_at=moment,
        )
        return order_id

    # ------------------------------------------------------------- 内部
    def _lock_order(self, order_id: int) -> None:
        """`SELECT ... FOR UPDATE` 锁住订单行；订单不存在直接 404。

        顺带解决了"幽灵订单"：以前要等 FK 约束报错才知道订单不存在，
        现在在追加事实之前就明确告知，错误信息也更准确。
        """
        locked = self._db.execute(
            sa.text(_LOCK_ORDER_SQL), {"order_id": order_id}
        ).scalar_one_or_none()
        if locked is None:
            raise ApiError(404, ErrCode.ORDER_NOT_FOUND, "订单不存在")

    def _assert_patch_keys(self, patch: Mapping[str, Any]) -> None:
        """patch 的键必须是 `orders` 的真实列（第 4 条语义差异的落点）。

        这个校验的价值不在"防用户"，而在**防我们自己**：改了 `status_patch()`
        却忘了加列时，真库会抛 `UndefinedColumn`，堆栈深在 SQL 执行器里很难读；
        在这里提前拦截，错误信息直接点名是哪个键。
        """
        unknown = set(patch) - _ORDER_COLUMNS
        if unknown:
            raise RepoProgrammingError(
                f"status patch 里出现 orders 表不存在的列：{sorted(unknown)}；"
                f"合法列见 app/models/order.py（这通常是状态机与迁移不同步）"
            )


# ---------------------------------------------------------------------------
# 只读聚合（供后台/统计复用；不参与状态机）
# ---------------------------------------------------------------------------
def count_inflight(db: Session, runner_id: int) -> int:
    """BR-16：某接单人的在途单数（DB 才是权威，Redis 只做召回期预过滤）。"""
    return int(
        db.execute(
            sa.select(sa.func.count())
            .select_from(Order.__table__)
            .where(Order.__table__.c.runner_id == runner_id)
            .where(Order.__table__.c.status == Status.IN_PROGRESS.value)
        ).scalar_one()
    )


def count_pending(db: Session, publisher_id: int) -> int:
    """BR-11：某发单人的待接单数。"""
    return int(
        db.execute(
            sa.select(sa.func.count())
            .select_from(Order.__table__)
            .where(Order.__table__.c.publisher_id == publisher_id)
            .where(Order.__table__.c.status == Status.PENDING.value)
        ).scalar_one()
    )


__all__ = [
    "OrderDraft",
    "RepoProgrammingError",
    "SqlOrderRepo",
    "count_inflight",
    "count_pending",
    "map_integrity_error",
]
