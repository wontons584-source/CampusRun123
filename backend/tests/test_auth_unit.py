"""认证原语的单元测试（T-07）：密码哈希 / 令牌 / 手机号摘要 / DTO 校验。

## 为什么这些用例值得单独存在

`test_integration_grab.py` 覆盖的是链路，而链路上任何一段"看起来能用"的加密
实现都能让集成用例通过 —— 例如：
- 把密码**明文**存进 `password_hash` 列，`verify_password` 改成 `==` 比较，
  所有集成用例照样全绿（登录成功、返回正确用户），但数据库一次泄露就是全站密码；
- 令牌不校验 `exp`，所有用例也全绿，直到某天发现两年前的令牌还能用；
- `mask_phone` 少写一个 `*`，任何人肉眼看不出来，但 D16 的脱敏口径已经破了。

这些判据只能在原语层逐条钉住，而且它们**零外部依赖**（不连库、不连 Redis），
跑一次不到 2 秒。
"""

from __future__ import annotations

import datetime

import pytest
from app.core.clock import UTC
from app.core.errors import ApiError, ErrCode
from app.core.security import (
    JWT_TV,
    bearer_token,
    create_access_token,
    decode_token,
    hash_password,
    mask_phone,
    phone_digest,
    token_subject,
    token_version_of,
    verify_password,
)
from app.modules.auth.repo import default_nickname
from app.schemas.auth import LoginRequest, RegisterRequest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------


def test_hash_and_verify_roundtrip() -> None:
    """正确密码通过；错误密码不通过。"""
    stored = hash_password("Passw0rd!")
    assert stored != "Passw0rd!", "落库的必须是哈希，不能是明文"
    assert stored.startswith("$2b$"), f"应是 bcrypt 格式，实际 {stored[:4]}"
    assert len(stored) == 60, f"bcrypt 哈希固定 60 字符，实际 {len(stored)}"
    assert verify_password("Passw0rd!", stored) is True
    assert verify_password("Passw0rd", stored) is False
    assert verify_password("passw0rd!", stored) is False


def test_same_password_gets_different_hashes() -> None:
    """同一密码两次哈希必须不同（盐是随机的）。

    若相同，说明实现里没加盐（或用了固定盐）—— 彩虹表可以一次打穿全站。
    校验仍然必须对两者都成立。
    """
    first = hash_password("same-password")
    second = hash_password("same-password")
    assert first != second, "两次哈希相同 ⇒ 没有随机盐"
    assert verify_password("same-password", first) is True
    assert verify_password("same-password", second) is True


@pytest.mark.parametrize(
    "stored",
    [None, "", "not-a-bcrypt-hash", "$2b$12$tooshort"],
    ids=["none", "empty", "garbage", "truncated"],
)
def test_verify_password_never_raises_on_bad_hash(stored: str | None) -> None:
    """哈希缺失/损坏 → `False`，不是 500。

    脏数据（历史迁移、被截断的列）不该让登录接口崩掉；而且区分"没设密码"与
    "密码错"会把"哪些手机号存在但没设密码"泄露出去。
    """
    assert verify_password("whatever", stored) is False


def test_hash_password_rejects_empty() -> None:
    """空密码 → `400/40009`（不该走到哈希）。"""
    with pytest.raises(ApiError) as exc_info:
        hash_password("")
    assert (exc_info.value.http_status, exc_info.value.code) == (400, ErrCode.INVALID_ARGUMENT)


def test_password_longer_than_bcrypt_limit_is_handled() -> None:
    """超过 bcrypt 72 字节上限：不抛错，且**前 72 字节相同则等价**。

    bcrypt 算法只看前 72 字节。若实现不显式截断，bcrypt>=4.1 会直接抛
    `ValueError`（接口 500）；若实现截断，则"72 字节之后的内容"不影响校验 ——
    这正是把 Pydantic 的 `PASSWORD_MAX` 定在 72 的原因（入口就拒掉）。
    """
    base = "a" * 72
    stored = hash_password(base + "X" * 10)
    assert verify_password(base + "Y" * 10, stored) is True, (
        "bcrypt 只看前 72 字节；入口已限制 72 位，这里确认底层行为可预期"
    )


# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------


def test_token_roundtrip_carries_subject_and_version() -> None:
    """令牌能解出 `sub` / `tv`（ADR-05 的即时失效依赖这两个字段）。"""
    token = create_access_token(42, token_version=7)
    payload = decode_token(token)
    assert token_subject(payload) == 42
    assert token_version_of(payload) == 7
    assert payload[JWT_TV] == 7


def test_expired_token_is_40102() -> None:
    """过期 → `401/40102`（前端据此静默刷新，而不是把用户踢出去）。"""
    past = datetime.datetime.now(tz=UTC) - datetime.timedelta(hours=2)
    token = create_access_token(1, now=past, ttl_minutes=1)
    with pytest.raises(ApiError) as exc_info:
        decode_token(token)
    assert (exc_info.value.http_status, exc_info.value.code) == (401, ErrCode.TOKEN_EXPIRED)


def test_tampered_token_is_40103() -> None:
    """签名被改 → `401/40103`（与"过期"分开，处理方式不同）。"""
    token = create_access_token(1)
    head, payload, _signature = token.split(".")
    forged = f"{head}.{payload}.{'A' * 43}"
    with pytest.raises(ApiError) as exc_info:
        decode_token(forged)
    assert (exc_info.value.http_status, exc_info.value.code) == (401, ErrCode.TOKEN_INVALID)


def test_token_with_non_numeric_subject_is_40103() -> None:
    """`sub` 不是数字 → `401/40103`，不是 `ValueError`/500。"""
    with pytest.raises(ApiError) as exc_info:
        token_subject({"sub": "abc"})
    assert exc_info.value.code == ErrCode.TOKEN_INVALID


def test_token_version_defaults_to_zero() -> None:
    """早期未带 `tv` 的令牌按 0 处理（兼容，不判非法）。"""
    assert token_version_of({}) == 0
    assert token_version_of({"tv": "3"}) == 3


@pytest.mark.parametrize(
    "header,expected_code",
    [
        (None, ErrCode.UNAUTHENTICATED),
        ("", ErrCode.UNAUTHENTICATED),
        ("Bearer", ErrCode.TOKEN_INVALID),
        ("Bearer   ", ErrCode.TOKEN_INVALID),
        ("Basic abc", ErrCode.TOKEN_INVALID),
    ],
    ids=["none", "empty", "scheme-only", "blank-token", "wrong-scheme"],
)
def test_bearer_token_rejects_malformed_headers(header: str | None, expected_code: int) -> None:
    """`Authorization` 头的各种残缺形态都要给出可识别的 401。"""
    with pytest.raises(ApiError) as exc_info:
        bearer_token(header)
    assert exc_info.value.code == expected_code


def test_bearer_token_accepts_case_insensitive_scheme() -> None:
    """`bearer` / `BEARER` 都接受（RFC 7235 规定 scheme 不区分大小写）。"""
    assert bearer_token("bearer abc.def.ghi") == "abc.def.ghi"
    assert bearer_token("  BEARER  abc.def.ghi  ") == "abc.def.ghi"


# ---------------------------------------------------------------------------
# 手机号
# ---------------------------------------------------------------------------


def test_phone_digest_is_deterministic_and_sized_for_index() -> None:
    """摘要必须确定性（否则登录查不到人）、且长度 ≤ 64（`provider_uid` 的列宽）。"""
    first = phone_digest("13912345678")
    assert first == phone_digest("13912345678")
    assert first == phone_digest(" 13912345678 "), "首尾空格应被归一化"
    assert len(first) == 64, f"sha256 十六进制应 64 字符，实际 {len(first)}"
    assert phone_digest("13912345679") != first


def test_mask_phone_hides_middle_four() -> None:
    """D16：只保留前 3 位与后 4 位。"""
    assert mask_phone("13912345678") == "139****5678"
    assert mask_phone("12345") == "123****2345"   # 非 11 位也按同一规则兜底


def test_default_nickname_uses_last_four_digits() -> None:
    """BR-03：默认「同学+尾号4位」。"""
    assert default_nickname("13912345678") == "同学5678"
    assert default_nickname("12") == "同学"


# ---------------------------------------------------------------------------
# DTO 校验（结构层，不碰数据库）
# ---------------------------------------------------------------------------


def test_register_request_normalizes_phone_with_separators() -> None:
    """带分隔符的手机号被归一化（用户从通讯录复制常带空格/短横线）。"""
    body = RegisterRequest(phone="139-1234-5678", password="Passw0rd!", nickname="  小A ")
    assert body.phone == "13912345678"
    assert body.nickname == "小A"


@pytest.mark.parametrize(
    "phone",
    ["12345678901", "1391234567", "139123456789", "139abc56789", ""],
    ids=["bad-prefix", "too-short", "too-long", "has-letters", "empty"],
)
def test_register_request_rejects_bad_phone(phone: str) -> None:
    """非法手机号在 DTO 层就被拒（40000），不进业务层。"""
    with pytest.raises(ValidationError):
        RegisterRequest(phone=phone, password="Passw0rd!")


@pytest.mark.parametrize("password", ["12345", "x" * 73, "   "], ids=["too-short", "too-long", "blank"])
def test_register_request_rejects_bad_password(password: str) -> None:
    """密码长度不在 6–72 就拒；全空白也拒（`"   "` 是 6 个字符但强度为 0）。"""
    with pytest.raises(ValidationError):
        RegisterRequest(phone="13912345678", password=password)


def test_login_request_accepts_formatted_phone() -> None:
    """登录同样做归一化，否则「注册用短横线、登录不用」会出现假性登录失败。"""
    assert LoginRequest(phone="139 1234 5678", password="x").phone == "13912345678"
