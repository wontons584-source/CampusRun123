"""init 14 tables

Revision ID: 0001
Revises:
Create Date: 2025-01-01 00:00:00.000000

T-03：Alembic 基线与首版全表迁移（**手写**，因为开发机没有数据库、无法 autogenerate）。

来源逐字对照 `docs/02-ARCHITECTURE.md`：
- 5.2 表结构（14 张，一张不多一张不少）
- 5.3 索引清单（27 条，其中 5 条为**部分索引**，无法自动生成 → 手写在下方并标注）
- 5.4 状态枚举（varchar 承载，不建 PG ENUM）

本迁移刻意包含、且**不允许**后续补丁迁移再加的字段（T-03 完成判据）：
- `orders.auto_confirm_due_at`（可空）：BR-21/BR-34 的 24h 到期时刻
- `orders.pre_dispute_status`（可空）：BR-35 挂起态语义
- `orders.pick_code_enc`（bytea 可空）：FR-ORDER-10 取件码密文

刻意**不建**：`orders(to_geohash)`、任何 GIST/PostGIS 地理索引（ADR-02），
以及 `orders(status, delivered_at)`（delivered_at 在 T14 回滚后不再是到期依据）。

downgrade() 按相反顺序 drop 表；索引随表一起删除（PostgreSQL 语义），
因此不再逐条 drop_index —— 显式 drop 索引只会让回滚脚本变长而不更安全。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.models.user import CREDIT_LEVEL_EXPR

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = postgresql.JSONB
BYTEA = postgresql.BYTEA


def upgrade() -> None:
    # ------------------------------------------------------------------ users
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("student_no", sa.String(length=20), nullable=True,
                  comment="BR-02 全局唯一；未认证为 NULL"),
        sa.Column("nickname", sa.String(length=32), nullable=False,
                  comment="默认「同学+尾号4位」"),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("phone_enc", BYTEA(), nullable=True,
                  comment="AES-GCM 密文，禁止进任何 DTO"),
        sa.Column("phone_mask", sa.String(length=11), nullable=True,
                  comment="138****8888，直接供接口返回"),
        sa.Column("dept", sa.String(length=64), nullable=True),
        sa.Column("grade", sa.String(length=8), nullable=True),
        sa.Column("verify_status", sa.String(length=16), server_default=sa.text("'UNVERIFIED'"),
                  nullable=False,
                  comment="UNVERIFIED/REVIEWING/APPROVED/REJECTED（FR-AUTH-04）"),
        sa.Column("credit_score", sa.Numeric(precision=5, scale=2),
                  server_default=sa.text("100"), nullable=False,
                  comment="BR-25 区间 0–120"),
        # GENERATED 列（BR-26）—— autogenerate 生成不出来，手写。
        # 表达式与 app/models/user.py 的 CREDIT_LEVEL_EXPR 同源，禁止在此另写一份。
        sa.Column("credit_level", sa.String(length=8),
                  sa.Computed(CREDIT_LEVEL_EXPR, persisted=True), nullable=False,
                  comment="GENERATED：EXCELLENT/GOOD/NORMAL/LIMITED（BR-26），禁止应用层写入"),
        sa.Column("status", sa.String(length=12), server_default=sa.text("'ACTIVE'"),
                  nullable=False, comment="ACTIVE/BANNED/DELETED"),
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False,
                  comment="ADR-05：+1 即全部旧 token 失效"),
        sa.Column("is_runner_mode", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False, comment="FR-ME-03 召回硬条件"),
        sa.Column("notify_settings", JSONB(), server_default=sa.text("'{}'::jsonb"),
                  nullable=False, comment="推送开关/免打扰时段"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.CheckConstraint("credit_score >= 0 AND credit_score <= 120",
                           name=op.f("ck_users_credit_score_range")),
        sa.CheckConstraint(
            "verify_status IN ('UNVERIFIED', 'REVIEWING', 'APPROVED', 'REJECTED')",
            name=op.f("ck_users_verify_status_enum")),
        sa.CheckConstraint("status IN ('ACTIVE', 'BANNED', 'DELETED')",
                           name=op.f("ck_users_status_enum")),
    )
    # 5.3：学号唯一校验（BR-02）、后台按学号检索
    op.create_index("uq_users_student_no", "users", ["student_no"], unique=True)
    # 5.3：后台认证待审队列（FR-ADMIN-02）
    op.create_index("ix_users_verify_status_created_at", "users",
                    ["verify_status", "created_at"], unique=False)

    # --------------------------------------------------------- auth_identities
    op.create_table(
        "auth_identities",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(length=12), nullable=False,
                  comment="PHONE / WECHAT"),
        sa.Column("provider_uid", sa.String(length=64), nullable=False,
                  comment="手机号哈希 / openid"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_auth_identities"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_auth_identities_user_id_users",
                                ondelete="CASCADE"),
        sa.CheckConstraint("provider IN ('PHONE', 'WECHAT')",
                           name=op.f("ck_auth_identities_provider_enum")),
    )
    # 5.3：登录查档（US-01）
    op.create_index("uq_auth_identities_provider_provider_uid", "auth_identities",
                    ["provider", "provider_uid"], unique=True)

    # ---------------------------------------------------- student_verifications
    op.create_table(
        "student_verifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.String(length=12), nullable=False, comment="EMAIL / OCR"),
        sa.Column("student_no_input", sa.String(length=20), nullable=True),
        sa.Column("name_input", sa.String(length=32), nullable=True),
        sa.Column("email", sa.String(length=64), nullable=True, comment="认证邮箱"),
        sa.Column("evidence_key", sa.Text(), nullable=True,
                  comment="对象存储 key；BR-05 审核通过后 30 天删原图"),
        sa.Column("status", sa.String(length=12), nullable=False,
                  comment="PENDING/APPROVED/REJECTED"),
        sa.Column("reject_reason", sa.String(length=200), nullable=True,
                  comment="US-03 必须给具体原因"),
        sa.Column("reviewer_id", sa.BigInteger(), nullable=True,
                  comment="审核管理员 users.id（不加 FK，避免与删除流程耦合）"),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_student_verifications"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name="fk_student_verifications_user_id_users",
                                ondelete="CASCADE"),
        sa.CheckConstraint("method IN ('EMAIL', 'OCR')",
                           name=op.f("ck_student_verifications_method_enum")),
        sa.CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED')",
                           name=op.f("ck_student_verifications_status_enum")),
    )
    # 5.3：查最新一次认证状态
    op.create_index("ix_student_verifications_user_id_created_at", "student_verifications",
                    ["user_id", sa.text("created_at DESC")], unique=False)
    # 5.3：审核队列
    op.create_index("ix_student_verifications_status_created_at", "student_verifications",
                    ["status", "created_at"], unique=False)

    # ------------------------------------------------------------------- pois
    op.create_table(
        "pois",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("campus_id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False,
                  comment="预留多校区（M1 不做）"),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False,
                  comment="STATION/DORM/CANTEEN/BUILDING/ADMIN/PRINT/SHOP"),
        sa.Column("lat", sa.Numeric(precision=9, scale=6), nullable=False,
                  comment="GCJ02（高德系）"),
        sa.Column("lng", sa.Numeric(precision=9, scale=6), nullable=False,
                  comment="GCJ02（高德系）"),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False,
                  comment="下架后新单不可选"),
        sa.Column("sort_hint", sa.Integer(), server_default=sa.text("0"), nullable=False,
                  comment="搜索排序权重"),
        sa.PrimaryKeyConstraint("id", name="pk_pois"),
        sa.CheckConstraint(
            "type IN ('STATION', 'DORM', 'CANTEEN', 'BUILDING', 'ADMIN', 'PRINT', 'SHOP')",
            name=op.f("ck_pois_type_enum")),
    )
    # 5.2：UNIQUE(campus_id, name)
    op.create_index("uq_pois_campus_id_name", "pois", ["campus_id", "name"], unique=True)
    # 5.3：字典列表/筛选（FR-ORDER-02）
    op.create_index("ix_pois_campus_id_is_active_type", "pois",
                    ["campus_id", "is_active", "type"], unique=False)

    # ----------------------------------------------------------------- orders
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_no", sa.String(length=20), nullable=False,
                  comment="CR + yyMMdd + 6 位随机（人读）"),
        sa.Column("publisher_id", sa.BigInteger(), nullable=False),
        sa.Column("runner_id", sa.BigInteger(), nullable=True, comment="抢单后写入"),
        sa.Column("type", sa.String(length=12), nullable=False,
                  comment="EXPRESS/BUY/SEND/OTHER（FR-ORDER-01）"),
        sa.Column("from_poi_id", sa.BigInteger(), nullable=False,
                  comment="BR-08 必须来自字典"),
        sa.Column("to_poi_id", sa.BigInteger(), nullable=False,
                  comment="BR-08 必须来自字典"),
        sa.Column("from_lat", sa.Numeric(precision=9, scale=6), nullable=False,
                  comment="冗余自 POI，避免召回时 join"),
        sa.Column("from_lng", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("to_lat", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("to_lng", sa.Numeric(precision=9, scale=6), nullable=False),
        sa.Column("reward", sa.Numeric(precision=6, scale=2), nullable=False,
                  comment="BR-07：2..50，.5 步进在 API 层校验"),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column("images", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False,
                  comment="最多 3 个 key（FR-ORDER-04）"),
        sa.Column("pick_code_enc", BYTEA(), nullable=True,
                  comment="FR-ORDER-10 取件码 AES-GCM 密文；type=EXPRESS 时非空；"
                          "禁止落日志/进 list DTO"),
        sa.Column("expect_minutes", sa.SmallInteger(), nullable=False,
                  comment="30/60/120/720（今日内→当日23:00）"),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False,
                  comment="到期未接 → EXPIRED（BR-09/T4）"),
        sa.Column("status", sa.String(length=12), nullable=False,
                  comment="7 值见 5.4；索引见 5.3 复合索引"),
        sa.Column("settle_state", sa.String(length=8),
                  server_default=sa.text("'UNSETTLED'"), nullable=False,
                  comment="UNSETTLED/SETTLED/FROZEN（C2：只记状态不记钱）"),
        sa.Column("push_round", sa.SmallInteger(), server_default=sa.text("0"), nullable=False,
                  comment="已推轮次，0=未推（BR-14）"),
        sa.Column("last_push_at", sa.DateTime(timezone=True), nullable=True,
                  comment="扩圈扫描依据"),
        sa.Column("is_late", sa.Boolean(), server_default=sa.text("false"), nullable=False,
                  comment="BR-23"),
        sa.Column("auto_confirmed", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False, comment="BR-21"),
        sa.Column("auto_confirm_due_at", sa.DateTime(timezone=True), nullable=True,
                  comment="BR-21/BR-34：24h 自动确认的到期时刻。DELIVER(T7) 时=now+24h；"
                          "T14 回滚到 DELIVERED 时重写为 now+24h。扫描只读此字段，不读 delivered_at"),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True,
                  comment="T2 抢单时刻"),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True,
                  comment="T7 送达时刻；写入后只读（K-12 履约时长基准）"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True,
                  comment="T9/T10 完成时刻；只用于终态判定/完成率分母/评价窗口起算（K-12）"),
        sa.Column("cancel_reason", sa.String(length=200), nullable=True),
        sa.Column("pre_dispute_status", sa.String(length=12), nullable=True,
                  comment="BR-35 挂起态语义：进 DISPUTED 时写投诉前状态，裁决无责按此回滚"
                          "（T13/T14），出 DISPUTED 后置 NULL。仅 status='DISPUTED' 时有意义"),
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False,
                  comment="后台编辑与进度并发校验；每次迁移 +1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.ForeignKeyConstraint(["publisher_id"], ["users.id"],
                                name="fk_orders_publisher_id_users", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["runner_id"], ["users.id"],
                                name="fk_orders_runner_id_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["from_poi_id"], ["pois.id"],
                                name="fk_orders_from_poi_id_pois", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["to_poi_id"], ["pois.id"],
                                name="fk_orders_to_poi_id_pois", ondelete="RESTRICT"),
        sa.CheckConstraint("reward >= 2 AND reward <= 50", name=op.f("ck_orders_reward_range")),
        sa.CheckConstraint("type IN ('EXPRESS', 'BUY', 'SEND', 'OTHER')",
                           name=op.f("ck_orders_type_enum")),
        sa.CheckConstraint(
            "status IN ('PENDING', 'IN_PROGRESS', 'DELIVERED', 'COMPLETED', "
            "'CANCELLED', 'EXPIRED', 'DISPUTED')",
            name=op.f("ck_orders_status_enum")),
        sa.CheckConstraint("settle_state IN ('UNSETTLED', 'SETTLED', 'FROZEN')",
                           name=op.f("ck_orders_settle_state_enum")),
        sa.CheckConstraint("type <> 'EXPRESS' OR pick_code_enc IS NOT NULL",
                           name=op.f("ck_orders_express_pick_code")),
    )
    # 5.3（部分索引 1/5）：过期扫描 T4（D12）、扩圈扫描
    op.create_index("ix_orders_status_deadline", "orders", ["status", "deadline"],
                    unique=False, postgresql_where=sa.text("status = 'PENDING'"))
    # 5.3：「我发布的」游标分页（FR-CENTER-01）
    op.create_index("ix_orders_publisher_id_created_at_id", "orders",
                    ["publisher_id", sa.text("created_at DESC"), sa.text("id DESC")],
                    unique=False)
    # 5.3：「我接的」+ 在途数统计（BR-16）
    op.create_index("ix_orders_runner_id_status_created_at_id", "orders",
                    ["runner_id", "status", sa.text("created_at DESC"), sa.text("id DESC")],
                    unique=False)
    # 5.3（部分索引 2/5）：接单记录与月收入（FR-CENTER-04）
    op.create_index("ix_orders_runner_id_completed_at", "orders",
                    ["runner_id", sa.text("completed_at DESC")],
                    unique=False, postgresql_where=sa.text("status = 'COMPLETED'"))
    # 5.3（部分索引 3/5）：24h 自动确认扫描（BR-21/D4）
    op.create_index("ix_orders_auto_confirm_due_at", "orders", ["auto_confirm_due_at"],
                    unique=False, postgresql_where=sa.text("status = 'DELIVERED'"))
    # 5.3：按订单号检索（US-20）
    op.create_index("uq_orders_order_no", "orders", ["order_no"], unique=True)
    # 5.3：后台时间范围检索、看板
    op.create_index("ix_orders_created_at", "orders", [sa.text("created_at DESC")],
                    unique=False)
    # 5.3（部分索引 4/5）：FR-FULFILL-06 静默预警扫描（T-24）
    op.create_index("ix_orders_accepted_at", "orders", ["accepted_at"],
                    unique=False, postgresql_where=sa.text("status = 'IN_PROGRESS'"))

    # --------------------------------------------------------- order_progress
    op.create_table(
        "order_progress",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.SmallInteger(), nullable=False, comment="单调递增，防乱序"),
        sa.Column("action", sa.String(length=20), nullable=False,
                  comment="见 5.4 的 18 个 action"),
        sa.Column("from_status", sa.String(length=12), nullable=False,
                  comment="迁移前状态，可重放"),
        sa.Column("to_status", sa.String(length=12), nullable=False,
                  comment="迁移后状态，可重放"),
        sa.Column("actor_id", sa.BigInteger(), nullable=True, comment="NULL = 系统"),
        sa.Column("actor_role", sa.String(length=8), nullable=False,
                  comment="PUBLISHER/RUNNER/ADMIN/SYSTEM"),
        sa.Column("note", sa.String(length=200), nullable=True,
                  comment="进度备注/催单/关闭原因"),
        sa.Column("images", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False,
                  comment="送达凭证（FR-FULFILL-03）"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_order_progress"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"],
                                name="fk_order_progress_order_id_orders", ondelete="CASCADE"),
        sa.CheckConstraint(
            "action IN ('CREATE', 'ACCEPT', 'PICKED_UP', 'ON_THE_WAY', 'NOTE', 'URGE', "
            "'DELIVER', 'CONFIRM', 'AUTO_CONFIRM', 'REQUEST_CANCEL', 'APPROVE_CANCEL', "
            "'REJECT_CANCEL', 'CANCEL', 'CANCEL_LOST', 'EXPIRE', 'DISPUTE_OPENED', "
            "'ARBITRATE_FREE', 'ARBITRATE_LIABLE', 'FORCE_CLOSE')",
            name=op.f("ck_order_progress_action_enum")),
        sa.CheckConstraint("actor_role IN ('PUBLISHER', 'RUNNER', 'ADMIN', 'SYSTEM')",
                           name=op.f("ck_order_progress_actor_role_enum")),
    )
    # 5.3：时间轴顺序读（FR-CENTER-03）+ 幂等去重
    op.create_index("uq_order_progress_order_id_seq", "order_progress", ["order_id", "seq"],
                    unique=True)

    # -------------------------------------------------- order_cancel_requests
    op.create_table(
        "order_cancel_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("requester_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False,
                  comment="PENDING/APPROVED/REJECTED"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_order_cancel_requests"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"],
                                name="fk_order_cancel_requests_order_id_orders",
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requester_id"], ["users.id"],
                                name="fk_order_cancel_requests_requester_id_users",
                                ondelete="CASCADE"),
        sa.CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED')",
                           name=op.f("ck_order_cancel_requests_status_enum")),
    )
    # 5.3（部分索引 5/5）：防重复申请 —— 「同一单同时只有一条待响应申请」
    op.create_index("uq_order_cancel_requests_order_id_pending", "order_cancel_requests",
                    ["order_id"], unique=True,
                    postgresql_where=sa.text("status = 'PENDING'"))

    # ---------------------------------------------------------------- reviews
    op.create_table(
        "reviews",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("from_id", sa.BigInteger(), nullable=False),
        sa.Column("to_id", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False, comment="P2R / R2P"),
        sa.Column("rating", sa.SmallInteger(), nullable=False,
                  comment="1..5；BR-27 一单一评价人一次"),
        sa.Column("tags", JSONB(), nullable=True, comment="预设标签（FR-REVIEW-03）"),
        sa.Column("content", sa.String(length=100), nullable=True),
        sa.Column("is_hidden", sa.Boolean(), server_default=sa.text("false"), nullable=False,
                  comment="隐私内容/冻结隐藏（BR-29）"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_reviews"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"],
                                name="fk_reviews_order_id_orders", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_id"], ["users.id"],
                                name="fk_reviews_from_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_id"], ["users.id"],
                                name="fk_reviews_to_id_users", ondelete="CASCADE"),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name=op.f("ck_reviews_rating_range")),
        sa.CheckConstraint("direction IN ('P2R', 'R2P')", name=op.f("ck_reviews_direction_enum")),
    )
    # 5.3：BR-27 一单一评价人一次
    op.create_index("uq_reviews_order_id_from_id", "reviews", ["order_id", "from_id"],
                    unique=True)
    # 5.3：个人主页近 20 条（FR-REVIEW-06）
    op.create_index("ix_reviews_to_id_created_at", "reviews",
                    ["to_id", sa.text("created_at DESC")], unique=False)

    # ------------------------------------------------------------ credit_logs
    op.create_table(
        "credit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("delta", sa.Numeric(precision=5, scale=2), nullable=False,
                  comment="正负均可"),
        sa.Column("score_after", sa.Numeric(precision=5, scale=2), nullable=False,
                  comment="变更后分值，供回溯"),
        sa.Column("reason_code", sa.String(length=24), nullable=False,
                  comment="ORDER_DONE/GOOD_REVIEW/LATE/ACCEPT_CANCEL/DISPUTE_LOST/"
                          "ADMIN_ADJUST"),
        sa.Column("order_id", sa.BigInteger(), nullable=True),
        sa.Column("operator_id", sa.BigInteger(), nullable=True,
                  comment="管理员调整时的 users.id（不加 FK，见 5.2）"),
        sa.Column("note", sa.String(length=200), nullable=True,
                  comment="管理员调整必填理由（US-21）"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_credit_logs"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name="fk_credit_logs_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"],
                                name="fk_credit_logs_order_id_orders", ondelete="SET NULL"),
        sa.CheckConstraint(
            "reason_code IN ('ORDER_DONE', 'GOOD_REVIEW', 'LATE', 'ACCEPT_CANCEL', "
            "'DISPUTE_LOST', 'ADMIN_ADJUST')",
            name=op.f("ck_credit_logs_reason_code_enum")),
    )
    # 5.3：信誉明细
    op.create_index("ix_credit_logs_user_id_created_at", "credit_logs",
                    ["user_id", sa.text("created_at DESC")], unique=False)

    # --------------------------------------------------------------- disputes
    op.create_table(
        "disputes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False,
                  comment="BR-28 一单一次（UNIQUE）"),
        sa.Column("initiator_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=24), nullable=False,
                  comment="NOT_DELIVERED/DAMAGED/MISMATCH/FAKE_DELIVER/BAD_ATTITUDE/"
                          "RUNNER_LOST"),
        sa.Column("evidence", JSONB(), nullable=True,
                  comment='{images:[...], text:\"\"} ≤5 图 ≤300 字；T16 免举证'),
        sa.Column("status", sa.String(length=14), nullable=False,
                  comment="M1: OPEN → RESOLVED；M3 加 EVIDENCE/ARBITRATING/APPEALED"),
        sa.Column("result", sa.String(length=14), nullable=True,
                  comment="FOR_PUBLISHER/FOR_RUNNER/PARTIAL"),
        sa.Column("result_note", sa.String(length=200), nullable=True),
        sa.Column("arbitrator_id", sa.BigInteger(), nullable=True,
                  comment="裁决管理员 users.id（不加 FK，见 5.2）"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_disputes"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"],
                                name="fk_disputes_order_id_orders", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["initiator_id"], ["users.id"],
                                name="fk_disputes_initiator_id_users", ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('OPEN', 'RESOLVED')", name=op.f("ck_disputes_status_enum")),
        sa.CheckConstraint(
            "reason IN ('NOT_DELIVERED', 'DAMAGED', 'MISMATCH', 'FAKE_DELIVER', "
            "'BAD_ATTITUDE', 'RUNNER_LOST')",
            name=op.f("ck_disputes_reason_enum")),
        sa.CheckConstraint(
            "result IS NULL OR result IN ('FOR_PUBLISHER', 'FOR_RUNNER', 'PARTIAL')",
            name=op.f("ck_disputes_result_enum")),
    )
    # 5.3：一单一次纠纷 + 纠纷队列
    op.create_index("uq_disputes_order_id", "disputes", ["order_id"], unique=True)
    op.create_index("ix_disputes_status_created_at", "disputes", ["status", "created_at"],
                    unique=False)

    # -------------------------------------------------------------- push_logs
    op.create_table(
        "push_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("runner_id", sa.BigInteger(), nullable=False),
        sa.Column("round", sa.SmallInteger(), nullable=False,
                  comment="扩圈轮次 1/2/3（BR-14）"),
        sa.Column("channel", sa.String(length=12), nullable=False,
                  comment="INBOX/PUSH/HALL_ONLY（降级也留痕，D17）"),
        sa.Column("distance_m", sa.Integer(), nullable=True, comment="到召回键距离（米）"),
        sa.Column("score", sa.Numeric(precision=6, scale=4), nullable=True,
                  comment="打分值，供调参（FR-PUSH-08）"),
        sa.Column("is_clicked", sa.Boolean(), server_default=sa.text("false"), nullable=False,
                  comment="效果回流"),
        sa.Column("is_grabbed", sa.Boolean(), server_default=sa.text("false"), nullable=False,
                  comment="效果回流"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_push_logs"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"],
                                name="fk_push_logs_order_id_orders", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["runner_id"], ["users.id"],
                                name="fk_push_logs_runner_id_users", ondelete="CASCADE"),
        sa.CheckConstraint("channel IN ('INBOX', 'PUSH', 'HALL_ONLY')",
                           name=op.f("ck_push_logs_channel_enum")),
    )
    # 5.3：频控审计与调参回查
    op.create_index("ix_push_logs_runner_id_created_at", "push_logs",
                    ["runner_id", sa.text("created_at DESC")], unique=False)
    # 5.3：扩圈排除已推人群（BR-14）
    op.create_index("ix_push_logs_order_id_round", "push_logs", ["order_id", "round"],
                    unique=False)

    # ---------------------------------------------------------- notifications
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False,
                  comment="PUSH_NEW_ORDER/ACCEPTED/PROGRESS/DELIVERED/NEED_CONFIRM/..."),
        sa.Column("title", sa.String(length=64), nullable=True),
        sa.Column("body", sa.String(length=200), nullable=True),
        sa.Column("payload", JSONB(), nullable=True, comment="深链参数 {order_id, tab}"),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True,
                  comment="红点 = read_at IS NULL 计数"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name="fk_notifications_user_id_users", ondelete="CASCADE"),
    )
    # 5.3：站内信列表与红点计数
    op.create_index("ix_notifications_user_id_read_at_created_at", "notifications",
                    ["user_id", "read_at", sa.text("created_at DESC")], unique=False)

    # ------------------------------------------------------------ sys_configs
    op.create_table(
        "sys_configs",
        sa.Column("key", sa.String(length=48), nullable=False, comment="如 push.radius_m"),
        sa.Column("value", JSONB(), nullable=False, comment="类型化读取"),
        sa.Column("updated_by", sa.BigInteger(), nullable=True,
                  comment="操作管理员 users.id（不加 FK，见 5.2）"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False, comment="UTC"),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False,
                  comment="每次改 +1，写审计"),
        sa.PrimaryKeyConstraint("key", name="pk_sys_configs"),
    )

    # ------------------------------------------------------ admin_audit_logs
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("admin_id", sa.BigInteger(), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=True,
                  comment="ORDER/USER/POI/CONFIG/DISPUTE"),
        sa.Column("target_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=True,
                  comment="FORCE_CLOSE/BAN/UNBAN/VERIFY_PASS/VERIFY_REJECT/CREDIT_ADJUST/"
                          "CONFIG_UPDATE/ARBITRATE_FREE/ARBITRATE_LIABLE/VIEW_PICK_CODE"),
        sa.Column("before", JSONB(), nullable=True, comment="改前值"),
        sa.Column("after", JSONB(), nullable=True, comment="改后值"),
        sa.Column("note", sa.String(length=200), nullable=True,
                  comment="强制关闭必填原因（BR-32）"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_admin_audit_logs"),
        sa.ForeignKeyConstraint(["admin_id"], ["users.id"],
                                name="fk_admin_audit_logs_admin_id_users",
                                ondelete="RESTRICT"),
    )
    # 5.3：追溯（D20）
    op.create_index("ix_admin_audit_logs_target_type_target_id_created_at",
                    "admin_audit_logs",
                    ["target_type", "target_id", sa.text("created_at DESC")], unique=False)
    op.create_index("ix_admin_audit_logs_admin_id_created_at", "admin_audit_logs",
                    ["admin_id", sa.text("created_at DESC")], unique=False)


def downgrade() -> None:
    # 与 upgrade 严格相反的顺序；索引随表删除（PostgreSQL 语义）。
    op.drop_table("admin_audit_logs")
    op.drop_table("sys_configs")
    op.drop_table("notifications")
    op.drop_table("push_logs")
    op.drop_table("disputes")
    op.drop_table("credit_logs")
    op.drop_table("reviews")
    op.drop_table("order_cancel_requests")
    op.drop_table("order_progress")
    op.drop_table("orders")
    op.drop_table("pois")
    op.drop_table("student_verifications")
    op.drop_table("auth_identities")
    op.drop_table("users")
