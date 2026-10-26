"""订单号生成器（T-13 的一部分）。

格式（5.2）：`CR` + `yyMMdd` + 6 位随机 → 共 12 字符，落在 `varchar(20)` 内。

为什么 6 位随机而不是序号：人读订单号要能口述、能抄写，序号会泄露单量
（竞争对手/用户能从 `CR260001000001` 反推今天的第几单）。6 位随机（100 万空间）
在同一天内撞号概率极低，且 `orders.order_no` 有 `UNIQUE` 约束兜底 ——
撞了由仓储层映射成 `40909 订单号重复请重试`，重试即可。
"""

from __future__ import annotations

import secrets
from datetime import datetime

from app.core.clock import UTC

_PREFIX: str = "CR"


def _random_suffix() -> str:
    """6 位随机数字（去前导零避免看起来像序号）。"""
    return f"{secrets.randbelow(1_000_000):06d}"


def gen_order_no(now: datetime | None = None) -> str:
    """生成一个订单号：`CR` + `yyMMdd` + 6 位随机。

    `now` 用于测试注入（缺省取当前 UTC）。日期取 UTC 还是校园时区？
    订单号是给人看的，日期段应该用**校园时区**（Asia/Shanghai）——
    否则深夜 00:30（UTC 16:30）的单会显示成"昨天"。
    """
    from app.core.clock import _campus_tz  # 复用同一时区换算，不另起 timedelta

    moment = now if now is not None else datetime.now(tz=UTC)
    local = moment.astimezone(_campus_tz())
    return f"{_PREFIX}{local:%y%m%d}{_random_suffix()}"


def is_valid_order_no(order_no: str) -> bool:
    """粗校验：`CR` + 6 位日期 + 6 位数字 = 14 字符。用于后台检索入参兜底。"""
    return (
        len(order_no) == 14
        and order_no.startswith(_PREFIX)
        and order_no[2:8].isdigit()  # 6 位日期
        and order_no[8:].isdigit()   # 6 位随机
    )


__all__ = ["gen_order_no", "is_valid_order_no"]
