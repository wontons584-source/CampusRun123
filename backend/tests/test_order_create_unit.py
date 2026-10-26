"""发单相关纯单元测试（不需要数据库）：订单号生成、加密、配置默认值。"""

from __future__ import annotations

import datetime

from app.core.clock import UTC
from app.core.security import (
    decrypt_pick_code,
    encrypt_pick_code,
    mask_phone,
)
from app.modules.admin.configs import DEFAULTS, ConfigService
from app.modules.order.no_gen import gen_order_no, is_valid_order_no


class TestOrderNo:
    def test_format(self) -> None:
        no = gen_order_no(datetime.datetime(2025, 6, 1, 2, 0, tzinfo=UTC))
        # CR(2) + yyMMdd(6) + 6 位随机 = 14 字符
        assert len(no) == 14
        assert no.startswith("CR")
        assert no[2:8] == "250601"  # 校园时区 2025-06-01 10:00 → 250601
        assert no[8:].isdigit()
        assert len(no[8:]) == 6

    def test_randomness(self) -> None:
        nos = {gen_order_no(datetime.datetime(2025, 6, 1, tzinfo=UTC)) for _ in range(50)}
        assert len(nos) > 1, "50 次生成不应全部相同"

    def test_valid(self) -> None:
        assert is_valid_order_no("CR250601123456")
        assert not is_valid_order_no("CR25060112345")   # 短
        assert not is_valid_order_no("X250601123456")   # 前缀错
        assert not is_valid_order_no("CR25060112345x")  # 非数字

    def test_date_uses_campus_tz(self) -> None:
        # UTC 16:30 = 北京次日 00:30，日期段应显示「次日」而非「今天」
        no = gen_order_no(datetime.datetime(2025, 6, 1, 16, 30, tzinfo=UTC))
        assert no[2:8] == "250602"


class TestSecurity:
    def test_roundtrip(self) -> None:
        enc = encrypt_pick_code("888888")
        assert b"888888" not in enc
        assert decrypt_pick_code(enc) == "888888"

    def test_tamper_detected(self) -> None:
        enc = bytearray(encrypt_pick_code("123456"))
        enc[-1] ^= 0xFF
        import pytest

        with pytest.raises(ValueError):
            decrypt_pick_code(bytes(enc))

    def test_mask(self) -> None:
        assert mask_phone("13812348888") == "138****8888"


class TestConfigDefaults:
    def test_all_defaults_present(self) -> None:
        # 每个业务阈值都有默认值，缺一个 cfg() 就回源不到
        required = [
            "dispute.silent_minutes",
            "dispute.silent_warn_minutes",
            "order.auto_confirm_hours",
            "grab.max_inflight",
            "urge.max_per_order",
        ]
        for key in required:
            assert key in DEFAULTS

    def test_cfg_unknown_key_returns_default(self) -> None:
        # 未知 key 不抛错，返回 None（无默认值）
        assert ConfigService(db=None).get("not.a.real.key") is None

    def test_cfg_default_fallback(self) -> None:
        # 已知 key 无 DB 无 Redis 时回落 DEFAULTS
        assert ConfigService(db=None).get("dispute.silent_minutes") == 45
