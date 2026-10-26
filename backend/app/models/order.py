"""订单聚合根：orders / order_progress / order_cancel_requests。

对应 02-ARCHITECTURE 5.2 / 5.3，以及 5.4「状态枚举 vs 事件流」的核心原则：

    `orders.status` 是**聚合结果**（可由事件重放），
    `order_progress` 是 **append-only 事实**（不可改不可删）。

两者在同一事务内写：先 append 事实，再
`UPDATE orders SET status=... WHERE id=? AND status=?`（CAS），一切以 CAS 成功为准。

几个极易写错的点（每条都对应文档里的一段血泪）：
- `pick_code_enc` 是 bytea 且**只在 EXPRESS 类型下非空**（FR-ORDER-10），
  数据库 CHECK + 应用层双保险；明文永不入库、永不进日志、永不进列表 DTO。
- `auto_confirm_due_at` 是 24h 自动确认的**唯一**依据（BR-21/BR-34）。
  **不要**用 `delivered_at` 判到期：T14 回滚时 `delivered_at` 是 3 天前，
  用它会导致「仲裁刚放行就被系统判完成」。
- `delivered_at` 写入后**只读**：它是 K-12 履约时长（`delivered_at - accepted_at`）
  的唯一基准。为此 **不建** `(status, delivered_at)` 索引 —— 索引一个永远不进
  WHERE 的列纯属写放大。
- `order_progress` 只继承 `CreatedAtMixin`：有 `updated_at` 就是在暗示可改。
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import BYTEA, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TimestampMixin

# K-03：订单 7 值状态枚举（varchar 承载，不建 PG ENUM）
ORDER_STATUSES: tuple[str, ...] = (
    "PENDING",
    "IN_PROGRESS",
    "DELIVERED",
    "COMPLETED",
    "CANCELLED",
    "EXPIRED",
    "DISPUTED",
)

# FR-ORDER-01：任务类型；OTHER 由 sys_configs 开关关闭（M1 默认关）
ORDER_TYPES: tuple[str, ...] = ("EXPRESS", "BUY", "SEND", "OTHER")

# C2：只记状态不记钱（平台不代收资金）
SETTLE_STATES: tuple[str, ...] = ("UNSETTLED", "SETTLED", "FROZEN")

# 5.4：progress.action 全集 **19 个**。
# 这里是**数据库层**的取值范围；状态机侧的 `Action` 枚举与之逐项对应
# （state_machine.py 内有断言测试，`test_qa_schema` 也有交叉校验）。
#
# 为什么是 19 而不是 5.4 正文数的 18：`URGE`（催单）在 5.4 的状态表里被列为
# `IN_PROGRESS` 的可执行动作，但当时的全集漏收了它 —— 那会导致「催单次数」
# （FR-FULFILL-05：每单最多 2 次）没有任何事实载体，只能放 Redis，
# 而 Redis 重启即清零，仲裁时无法举证「他催了几次」。故补入。
# `REVIEW`（评价）**刻意不收**：它的唯一载体是 `reviews` 表（有
# `UNIQUE(order_id, from_id)`），再写一条 progress 事件是重复事实。
PROGRESS_ACTIONS: tuple[str, ...] = (
    "CREATE",
    "ACCEPT",
    "PICKED_UP",
    "ON_THE_WAY",
    "NOTE",
    "URGE",
    "DELIVER",
    "CONFIRM",
    "AUTO_CONFIRM",
    "REQUEST_CANCEL",
    "APPROVE_CANCEL",
    "REJECT_CANCEL",
    "CANCEL",
    "CANCEL_LOST",
    "EXPIRE",
    "DISPUTE_OPENED",
    "ARBITRATE_FREE",
    "ARBITRATE_LIABLE",
    "FORCE_CLOSE",
)

ACTOR_ROLES: tuple[str, ...] = ("PUBLISHER", "RUNNER", "ADMIN", "SYSTEM")

CANCEL_REQUEST_STATUSES: tuple[str, ...] = ("PENDING", "APPROVED", "REJECTED")

# BR-07：酬劳区间；`.5` 步进在 API 层校验（DB 只挡越界）
REWARD_MIN: int = 2
REWARD_MAX: int = 50


class Order(Base, CreatedAtMixin):
    """订单主表（5.2 orders，31 列）。

    `created_at` 走 `CreatedAtMixin`；本表**没有** `updated_at`（文档 5.2 未列），
    可变性全部由 `version` 列（后台编辑与进度并发校验）承载。
    """

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="CR + yyMMdd + 6 位随机（人读）"
    )
    publisher_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_orders_publisher_id_users"),
        nullable=False,
    )
    runner_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL", name="fk_orders_runner_id_users"),
        nullable=True,
        comment="抢单后写入",
    )
    type: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        comment="EXPRESS/BUY/SEND/OTHER（FR-ORDER-01）",
    )
    from_poi_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("pois.id", ondelete="RESTRICT", name="fk_orders_from_poi_id_pois"),
        nullable=False,
        comment="BR-08 必须来自字典",
    )
    to_poi_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("pois.id", ondelete="RESTRICT", name="fk_orders_to_poi_id_pois"),
        nullable=False,
        comment="BR-08 必须来自字典",
    )
    from_lat: Mapped[Any] = mapped_column(
        Numeric(9, 6), nullable=False, comment="冗余自 POI，避免召回时 join"
    )
    from_lng: Mapped[Any] = mapped_column(Numeric(9, 6), nullable=False)
    to_lat: Mapped[Any] = mapped_column(Numeric(9, 6), nullable=False)
    to_lng: Mapped[Any] = mapped_column(Numeric(9, 6), nullable=False)
    reward: Mapped[Any] = mapped_column(
        Numeric(6, 2), nullable=False, comment="BR-07：2..50，.5 步进在 API 层校验"
    )
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    images: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="最多 3 个 key（FR-ORDER-04）",
    )
    pick_code_enc: Mapped[bytes | None] = mapped_column(
        BYTEA,
        nullable=True,
        comment="FR-ORDER-10 取件码 AES-GCM 密文；type=EXPRESS 时非空；禁止落日志/进 list DTO",
    )
    expect_minutes: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="30/60/120/720（今日内→当日23:00）"
    )
    deadline: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="到期未接 → EXPIRED（BR-09/T4）"
    )
    status: Mapped[str] = mapped_column(
        # 列宽 16（不是 5.2 写的 12）：varchar(12) 装 `IN_PROGRESS`(11) 只剩 1 个字符，
        # 一旦 M2 加更长的状态（如 `PARTIALLY_REFUNDED`），**全站订单写入**立刻挂。
        # 这类"每次写都要碰"的枚举列一律留足余量（当前余量 5）。
        String(16), nullable=False, comment="7 值见 5.4；索引见 5.3 复合索引"
    )
    settle_state: Mapped[str] = mapped_column(
        # 列宽 16 而不是 8：`'UNSETTLED'` 本身就有 9 个字符，而 5.2 原文写的
        # varchar(8) 会让**任何 insert 直接失败**（它是 DEFAULT，躲不过去）。
        # 这是"文档写错、只有真库能暴露"的第一类缺陷（单元测试用的内存替身
        # 不校验列宽，所以 524 个用例全绿也没发现）。
        # 放宽到 16 的取舍：枚举值长度随业务演进只会变长（如 `PARTIALLY_SETTLED`），
        # PG 里 varchar(n) 与 text 的存储完全一样、只有长度校验，放宽零成本；
        # 但**不要改成 text** —— 列宽是最后一道数据校验，去掉它等于允许任意垃圾值
        # 写进一个语义上只有 3 个取值的列。
        String(16),
        nullable=False,
        server_default=text("'UNSETTLED'"),
        comment="UNSETTLED/SETTLED/FROZEN（C2：只记状态不记钱）",
    )
    push_round: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("0"),
        comment="已推轮次，0=未推（BR-14）",
    )
    last_push_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="扩圈扫描依据"
    )
    is_late: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="BR-23"
    )
    auto_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="BR-21"
    )
    auto_confirm_due_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment=(
            "BR-21/BR-34：24h 自动确认的到期时刻。DELIVER(T7) 时=now+24h；"
            "T14 回滚到 DELIVERED 时重写为 now+24h。扫描只读此字段，不读 delivered_at"
        ),
    )
    accepted_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="T2 抢单时刻"
    )
    delivered_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="T7 送达时刻；写入后只读（K-12 履约时长基准）",
    )
    completed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="T9/T10 完成时刻；只用于终态判定/完成率分母/评价窗口起算（K-12）",
    )
    cancel_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pre_dispute_status: Mapped[str | None] = mapped_column(
        # 存的是订单状态，与 orders.status 同宽（varchar(12) 时余量只有 1）
        String(16),
        nullable=True,
        comment=(
            "BR-35 挂起态语义：进 DISPUTED 时写投诉前状态，裁决无责按此回滚"
            "（T13/T14），出 DISPUTED 后置 NULL。仅 status='DISPUTED' 时有意义"
        ),
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        comment="后台编辑与进度并发校验；每次迁移 +1",
    )

    __table_args__ = (
        # --- 5.3 索引清单（orders 共 8 条）---
        # 过期扫描 T4（D12）、扩圈扫描
        Index(
            "ix_orders_status_deadline",
            "status",
            "deadline",
            postgresql_where=text("status = 'PENDING'"),
        ),
        # 「我发布的」游标分页（FR-CENTER-01）
        Index(
            "ix_orders_publisher_id_created_at_id",
            "publisher_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # 「我接的」+ 在途数统计（BR-16）
        Index(
            "ix_orders_runner_id_status_created_at_id",
            "runner_id",
            "status",
            text("created_at DESC"),
            text("id DESC"),
        ),
        # 接单记录与月收入（FR-CENTER-04）
        Index(
            "ix_orders_runner_id_completed_at",
            "runner_id",
            text("completed_at DESC"),
            postgresql_where=text("status = 'COMPLETED'"),
        ),
        # 24h 自动确认扫描（BR-21/D4）。**刻意不建 (status, delivered_at)**
        Index(
            "ix_orders_auto_confirm_due_at",
            "auto_confirm_due_at",
            postgresql_where=text("status = 'DELIVERED'"),
        ),
        # 按订单号检索（US-20）
        Index("uq_orders_order_no", "order_no", unique=True),
        # 后台时间范围检索、看板
        Index("ix_orders_created_at", text("created_at DESC")),
        # FR-FULFILL-06 静默预警扫描（T-24）；T16/T17 守卫**不依赖**本索引
        Index(
            "ix_orders_accepted_at",
            "accepted_at",
            postgresql_where=text("status = 'IN_PROGRESS'"),
        ),
        # --- 约束 ---
        CheckConstraint("reward >= 2 AND reward <= 50", name="reward_range"),
        CheckConstraint(
            "type IN ('EXPRESS', 'BUY', 'SEND', 'OTHER')", name="type_enum"
        ),
        CheckConstraint(
            "status IN ('PENDING', 'IN_PROGRESS', 'DELIVERED', 'COMPLETED', "
            "'CANCELLED', 'EXPIRED', 'DISPUTED')",
            name="status_enum",
        ),
        CheckConstraint(
            "settle_state IN ('UNSETTLED', 'SETTLED', 'FROZEN')", name="settle_state_enum"
        ),
        # FR-ORDER-10：代取快递必须有取件码（应用层 + DB 双保险）
        CheckConstraint(
            "type <> 'EXPRESS' OR pick_code_enc IS NOT NULL", name="express_pick_code"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 严禁输出 pick_code_enc
        return f"<Order id={self.id} no={self.order_no} status={self.status}>"


class OrderProgress(Base, CreatedAtMixin):
    """订单事件流（5.2 order_progress）—— **append-only，不 UPDATE 不 DELETE**。

    只继承 `CreatedAtMixin`：`updated_at` 会暗示这行可改，与 append-only 语义冲突。

    `seq` 单调递增 + `UNIQUE(order_id, seq)`：
    ① 时间轴顺序读（FR-CENTER-03）② 幂等去重 ③ 投影对账的顺序基准。
    `seq` 与 `created_at` 严格同序，因此不必再建 `(order_id, created_at DESC)`。
    """

    __tablename__ = "order_progress"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE", name="fk_order_progress_order_id_orders"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="单调递增，防乱序"
    )
    action: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="见 5.4 的 19 个 action"
    )
    # `from_status` / `to_status` 是**最危险的两个列**：每写一条进度事件都要碰它们。
    # `varchar(12)` 装 `IN_PROGRESS`(11) 只剩 1 个字符 —— 加一个更长的状态，
    # 挂的不是某一条记录，而是**所有事件写入**（含发单、抢单、送达、投诉）。
    # 故与 orders.status 同宽 varchar(16)，当前余量 5。
    from_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="迁移前状态，可重放"
    )
    to_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="迁移后状态，可重放"
    )
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="NULL = 系统"
    )
    actor_role: Mapped[str] = mapped_column(
        # 原 varchar(8) 装不下 `'PUBLISHER'`(9) —— 发单人产生的任何事件都写不进去。
        # 同上放宽到 16：`ADMIN`/`SYSTEM`/`RUNNER` 都 ≤6，但 `PUBLISHER` 是 9，
        # 且将来可能加 `ARBITRATOR`(10) 之类的角色。
        String(16), nullable=False, comment="PUBLISHER/RUNNER/ADMIN/SYSTEM"
    )
    note: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="进度备注/催单/关闭原因"
    )
    images: Mapped[list[Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        comment="送达凭证（FR-FULFILL-03）",
    )

    __table_args__ = (
        # 5.3：时间轴顺序读（FR-CENTER-03）+ 幂等去重
        Index("uq_order_progress_order_id_seq", "order_id", "seq", unique=True),
        CheckConstraint(
            "action IN ('CREATE', 'ACCEPT', 'PICKED_UP', 'ON_THE_WAY', 'NOTE', 'URGE', "
            "'DELIVER', 'CONFIRM', 'AUTO_CONFIRM', 'REQUEST_CANCEL', 'APPROVE_CANCEL', "
            "'REJECT_CANCEL', 'CANCEL', 'CANCEL_LOST', 'EXPIRE', 'DISPUTE_OPENED', "
            "'ARBITRATE_FREE', 'ARBITRATE_LIABLE', 'FORCE_CLOSE')",
            name="action_enum",
        ),
        CheckConstraint(
            "actor_role IN ('PUBLISHER', 'RUNNER', 'ADMIN', 'SYSTEM')",
            name="actor_role_enum",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<OrderProgress order_id={self.order_id} seq={self.seq} "
            f"action={self.action} {self.from_status}->{self.to_status}>"
        )


class OrderCancelRequest(Base, TimestampMixin):
    """协商取消申请（5.2 order_cancel_requests，T8）。

    `UNIQUE(order_id) WHERE status='PENDING'`（部分唯一索引）保证同一单
    同时只有一条待响应申请 —— 这是「防重复申请」的唯一手段，
    靠应用层 `SELECT ... then INSERT` 在并发下必然漏。
    """

    __tablename__ = "order_cancel_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "orders.id",
            ondelete="CASCADE",
            name="fk_order_cancel_requests_order_id_orders",
        ),
        nullable=False,
    )
    requester_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.id",
            ondelete="CASCADE",
            name="fk_order_cancel_requests_requester_id_users",
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        # 列宽 16（不是 5.2 写的 10）：varchar(10) 装 `APPROVED`(8) 只剩 2 个字符
        String(16), nullable=False, comment="PENDING/APPROVED/REJECTED"
    )

    __table_args__ = (
        # 5.3：防重复申请（部分唯一索引，无法自动生成，手写在 0001_init.py）
        Index(
            "uq_order_cancel_requests_order_id_pending",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED')", name="status_enum"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<OrderCancelRequest order_id={self.order_id} status={self.status}>"
