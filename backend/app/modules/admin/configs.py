"""参数化配置（T-06, K-11）：`sys_configs` 表 + Redis 快照 60s 热生效。

## 设计（为什么这么分）

- **DB 是唯一权威**：`sys_configs` 表是参数的真值，后台改参数 = `UPDATE sys_configs`。
- **Redis 是读缓存**：`cfg("dispute.silent_minutes")` 先查 Redis，未命中/过期才回源 DB
  并回填快照（TTL 60s）。这样改一个参数 ≤60s 全站生效，**且不产生 per-order 查库**——
  否则发单/抢单这种热路径每次都要 `SELECT ... FROM sys_configs`。
- **读多写少**：参数在运营手里一天改不了几次，但订单链路每秒都在读。

## 阈值常量不落在 state_machine.py

`Thresholds` 的默认值只供单测；**生产里的真值来自本模块**。业务代码一律
`cfg("dispute.silent_minutes")`，不许把 `45` 写进状态机字面量（见 7.4）。

## 缺失键的行为

`cfg()` 对未知 key 返回 `DEFAULTS` 里的默认值并告警日志，**而不是抛错**：
一个没种子的参数不该让整条发单链路 500 —— 默认值兜底 + 告警，让运营补种子。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.governance import SysConfig

logger = logging.getLogger("campusrun.config")

#: 参数默认值（种子数据与 `cfg()` 兜底共用，唯一出处）。
#: 与 `state_machine.Thresholds` 的默认值逐项一致，改这里要同步改那里（有测试守着）。
DEFAULTS: Final[dict[str, Any]] = {
    # 纠纷 / 静默（BR-28 / BR-33 / BR-35）
    "dispute.silent_minutes": 45,
    "dispute.silent_warn_minutes": 40,
    "dispute.window_hours": 48,
    # 订单
    "order.auto_confirm_hours": 24,
    "order.send_window_start_hour": 7,
    "order.send_window_end_hour": 23,
    # 评价
    "review.window_days": 7,
    # 协商取消
    "cancel.request_ttl_minutes": 30,
    # 抢单 / 上限
    "grab.max_inflight": 3,
    "grab.max_pending_per_publisher": 3,
    # 催单
    "urge.max_per_order": 2,
    # 推送
    "push.radius_m": 800,
    "push.top_n": 20,
}

#: Redis 快照 TTL（秒）。改参数后 ≤60s 全站生效（US-22）。
_SNAPSHOT_TTL: Final[int] = 60

#: Redis 里快照键的前缀。`{key}` 是配置键（如 `dispute.silent_minutes`）。
_SNAPSHOT_PREFIX: Final[str] = "cfg:"


def _snapshot_key(key: str) -> str:
    return f"{_SNAPSHOT_PREFIX}{key}"


def _redis() -> Any:
    """惰性拿 Redis 客户端（`decode_responses=True` 方便直接存 JSON 字符串）。"""
    from app.db.redis import get_redis as _get

    return _get()


def _encode(value: Any) -> str:
    """配置值 → Redis 字符串。统一 JSON，避免 int/bool/float 的隐式类型坑。"""
    return json.dumps(value, ensure_ascii=False)


def _decode(raw: str | bytes) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


class ConfigService:
    """参数读写的统一入口。

    读路径：Redis 快照（TTL 60s）→ 回源 DB → 回填快照。
    写路径：`UPDATE sys_configs SET value=..., version=version+1`（DB 权威），
    然后**删快照**（下次读自动回源，≤60s 生效的另一种更快的形态）。
    """

    def __init__(self, db: Session | None = None) -> None:
        self._db = db

    # ------------------------------------------------------------------ 读
    def get(self, key: str, default: Any = None) -> Any:
        """读一个参数。顺序：Redis 快照 → DB → DEFAULTS。

        未知 key 返回 `DEFAULTS` 兜底（若也无则返回 `default`），不抛错。
        """
        # 1. Redis 快照
        try:
            cached = _redis().get(_snapshot_key(key))
            if cached is not None:
                return _decode(cached)
        except Exception:  # noqa: BLE001 - Redis 挂时静默降级到 DB，不打断业务
            logger.warning("config: Redis 快照读取失败，回源 DB（key=%s）", key)

        # 2. DB（权威）
        if self._db is not None:
            row = self._db.execute(
                sa.select(SysConfig.value).where(SysConfig.key == key)
            ).scalar_one_or_none()
            if row is not None:
                # 回填快照
                self._cache(key, row)
                return row

        # 3. 默认值兜底
        if key in DEFAULTS:
            return DEFAULTS[key]
        return default

    def get_int(self, key: str) -> int:
        return int(self.get(key))

    def get_float(self, key: str) -> float:
        return float(self.get(key))

    def get_str(self, key: str) -> str:
        return str(self.get(key))

    def get_bool(self, key: str) -> bool:
        return bool(self.get(key))

    # ------------------------------------------------------------------ 写
    def set(self, key: str, value: Any, *, updated_by: int | None = None) -> int:
        """更新一个参数：DB `version+1`，删快照（下次读回源）。返回新 version。

        审计（`CONFIG_UPDATE` + before/after）由调用方（admin service）写，
        本方法只负责「值 + version」的正确性 —— 单一职责。
        """
        if self._db is None:
            raise RuntimeError("ConfigService.set 需要绑定 Session")
        existing = self._db.execute(
            sa.select(SysConfig).where(SysConfig.key == key)
        ).scalar_one_or_none()
        if existing is None:
            # 未种子的参数首次写：插入 version=1
            self._db.add(SysConfig(key=key, value=value, updated_by=updated_by, version=1))
            new_version = 1
        else:
            existing.value = value
            existing.version += 1
            existing.updated_by = updated_by
            new_version = existing.version
        self._invalidate(key)
        return new_version

    # ------------------------------------------------------------------ 缓存
    def _cache(self, key: str, value: Any) -> None:
        try:
            _redis().set(_snapshot_key(key), _encode(value), ex=_SNAPSHOT_TTL)
        except Exception:  # noqa: BLE001
            pass  # 缓存写失败不影响正确性，只是下次还要回源

    def _invalidate(self, key: str) -> None:
        try:
            _redis().delete(_snapshot_key(key))
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 便捷入口（业务代码用，不每次 new ConfigService）
# ---------------------------------------------------------------------------
def cfg(key: str, default: Any = None) -> Any:
    """全局便捷读：不带 Session 的快速读取（Redis 命中则零 DB 往返）。"""
    return ConfigService(db=None).get(key, default=default)


__all__ = [
    "ConfigService",
    "DEFAULTS",
    "cfg",
]
