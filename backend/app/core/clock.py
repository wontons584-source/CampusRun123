"""时钟（T-04, K-04）。

**业务代码里禁止出现裸 `datetime.now()`**，一律注入 `Clock`：
- 统一 UTC aware（K-04：DB 一律 timestamptz 存 UTC）；
- 测试可注入固定时刻，让「44 分 59 秒拒 / 45 分 00 秒过」这类边界用例
  不需要 `sleep`（sleep 会让单测从 0.2s 变 90s，且仍然不可靠）；
- 校时区换算集中一处（`to_local`），避免每个模块自己 `timedelta(hours=8)`
  —— 一旦哪天要支持多校区不同时区，改一个地方就够。

为什么不用 `freezegun`：它 monkeypatch 标准库，对 `datetime.now()` 的
C 层调用在某些路径上无效，而且会让「有没有注入 Clock」这个设计约束失效。
显式注入是可测试性的正解。
"""

from __future__ import annotations

import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

UTC: datetime.tzinfo = datetime.UTC

# 校园所在时区（K-04）：所有「07:00 发单窗」「今日内 23:00」的判断都用它
CAMPUS_TZ_NAME: str = "Asia/Shanghai"


@lru_cache(maxsize=1)
def _campus_tz() -> datetime.tzinfo:
    """惰性加载时区，避免 import 期读文件（C1：无依赖也能 import）。

    Windows 上如果没有安装 `tzdata`，`ZoneInfo` 会抛 `ZoneInfoNotFoundError`；
    此时退化为固定 +08:00（中国无夏令时，固定偏移与 Asia/Shanghai 等价，
    只有历史时刻会差 1 小时，而业务只判「今天几点」）。
    """
    try:
        return ZoneInfo(CAMPUS_TZ_NAME)
    except Exception:  # noqa: BLE001 - 缺 tzdata 时退化为固定偏移
        return datetime.timezone(datetime.timedelta(hours=8), name="UTC+08:00")


def _as_utc(dt: datetime.datetime) -> datetime.datetime:
    """把任意 datetime 规范成 UTC aware。

    naive datetime 视为 UTC（DB 里只有 UTC，应用层不该产生 naive）；
    这是「最不坏」的约定：naive 值多来自 `datetime.utcnow()`，本来就想要 UTC。
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class Clock:
    """可注入的时钟。

    - `Clock()`：真实时钟，`now()` 返回当前 UTC aware 时刻。
    - `Clock(fixed=dt)`：固定时钟，`now()` 永远返回 `dt`（测试用）。
    - `advance(delta)`：固定时钟手动前进，模拟「45 分钟过去了」。
    """

    __slots__ = ("_fixed",)

    def __init__(self, fixed: datetime.datetime | None = None) -> None:
        self._fixed: datetime.datetime | None = _as_utc(fixed) if fixed is not None else None

    # ---------------- 读 ----------------
    def now(self) -> datetime.datetime:
        """当前时刻，**永远 UTC aware**。"""
        if self._fixed is not None:
            return self._fixed
        return datetime.datetime.now(tz=UTC)

    def today_local(self) -> datetime.date:
        """校园时区的「今天」—— 发单时间窗（BR-10）与「今日内」档位用它。"""
        return self.to_local(self.now()).date()

    def to_local(self, dt: datetime.datetime) -> datetime.datetime:
        """UTC（或任意 aware）→ 校园时区 aware。"""
        return _as_utc(dt).astimezone(_campus_tz())

    def to_utc(self, dt: datetime.datetime) -> datetime.datetime:
        return _as_utc(dt)

    # ---------------- 控制的（仅测试 / 任务重放） ----------------
    def freeze(self, at: datetime.datetime | None = None) -> None:
        """冻结在当前时刻（或指定时刻）。"""
        self._fixed = _as_utc(at) if at is not None else self.now()

    def advance(self, delta: datetime.timedelta) -> None:
        """固定时钟前进。未冻结时先冻结再加，保证语义可预期。"""
        base = self._fixed if self._fixed is not None else self.now()
        self._fixed = base + delta

    def reset(self) -> None:
        """恢复真实时钟。"""
        self._fixed = None

    @property
    def is_fixed(self) -> bool:
        return self._fixed is not None

    def __repr__(self) -> str:  # pragma: no cover
        mode = f"fixed={self._fixed.isoformat()}" if self._fixed else "system"
        return f"<Clock {mode}>"


# 进程级默认时钟。业务代码用 `from app.core.clock import clock`；
# 需要注入固定时间的单测**不要**改这个全局对象（会串测试），
# 而是构造 `Clock(fixed=...)` 传进被测函数。
clock: Clock = Clock()


def utcnow() -> datetime.datetime:
    """无状态便捷函数（等价 `clock.now()`），供只读场景使用。"""
    return clock.now()
