"""Celery 应用实例。

为什么现在就要有这个文件：docker-compose 的 worker / beat 两个服务都以
`app.workers.celery_app:celery_app` 作为入口，缺了它容器直接 ImportError 退出，
M0 的"五容器 healthy"永远达不到。任务本身在 T-18/T-19/T-24 才填，这里只
保证骨架可启动。

任务队列的边界（C3 单人开发）：
- 只承担**推送分发、定时扫描、housekeeping** 三类，不做事务消息。
- 任何需要"和数据库同事务"的逻辑都不能放进来，否则失败即丢任务。
"""

from __future__ import annotations

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "campusrun",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        # 任务模块在后续任务中逐个登记，登记后 beat 才能调度
        # "app.workers.tasks.push",     # T-19
        # "app.workers.tasks.sweep",    # T-24 / T-26 / T-27
    ],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_acks_late=True,                 # worker 崩溃时任务重新入队
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,        # 长任务场景下避免一个 worker 囤一堆
    broker_connection_retry_on_startup=True,
    result_expires=3600,
)

# ---------------------------------------------------------------------------
# 定时任务（M0 先留空，避免 beat 调度到不存在的任务而反复报错）
#
# T-19 会加：
#   "push.expand"          每 60s   扩圈扫描（BR-14）
# T-24/T-26/T-27 会加：
#   "sweep.silent_warn"    每 60s   静默预警（BR-35 / FR-FULFILL-06）
#   "sweep.expire"         每 60s   过期订单 T4
#   "sweep.auto_confirm"   每 60s   24h 自动确认 T10（读 auto_confirm_due_at）
#   "sweep.beat_heartbeat" 每 60s   beat 存活自检（D21 可观测）
# ---------------------------------------------------------------------------
celery_app.conf.beat_schedule: dict = {}

# 兜底：beat 进程若被误配成调度一个不存在的任务，会每 60s 刷一次 KeyError。
# 这里显式声明允许的任务名集合，未注册的直接忽略而不是崩溃。
celery_app.conf.task_ignore_result = False
celery_app.conf.beat_max_loop_interval = 30
celery_app.conf.broker_transport_options = {"visibility_timeout": 3600}

__all__ = ["celery_app"]
