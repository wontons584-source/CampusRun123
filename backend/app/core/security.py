"""字段级加密与令牌工具（T-07/T-13 依赖，提前在此实现）。

## 加密体系（K-08）

两个 AES-GCM 密钥各管一段：
- `phone_aes_key` → `users.phone_enc`（手机号脱敏，D16）
- `pick_code_aes_key` → `orders.pick_code_enc`（取件码，FR-ORDER-10）

**为什么手机号与取件码各用一把独立密钥**：两者的暴露面不同 —— 手机号在
`/me` 接口、认证流程里会频繁解密；取件码只在「详情页解锁」那一瞬间解密。
共用一把会让低频的取件码被高频的手机号流量"带着"暴露。独立密钥 = 独立轮换节奏。

**AES-GCM（不是 CBC）**：GCM 自带完整性校验（AEAD），密文被篡改会解密失败
而不是解出乱码。取件码一旦被改，宁可不返回也不返回错码。

## 密钥来源

环境变量 `PICK_CODE_AES_KEY` / `PHONE_AES_KEY`，是 **base64 编码的 32 字节**。
开发环境留空时用派生出的固定密钥（仅 dev，生产必须显式配置 —— `is_production`
时用空密钥会抛错，见 `_key_or_die`）。
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

# AES-GCM 标准 nonce 长度（96 bit）。每次加密随机生成，密文里前置。
_NONCE_LEN: Final[int] = 12

# 密文布局：nonce(12) + ciphertext（含 16 字节 GCM tag）
_MAGIC: Final[bytes] = b"v1:"  # 版本前缀，将来换算法/轮换密钥时能识别旧格式


def _key_or_die(raw: str, *, name: str) -> bytes:
    """把 base64 密钥解成 32 字节；生产环境缺失/非法直接抛错（fail-fast）。

    开发环境（非 production）用空密钥时派生一个稳定占位密钥，
    保证「不改 .env 也能跑测试」；但生产绝不允许空密钥静默降级 ——
    那等于所有取件码用同一把公开密钥加密。
    """
    if raw:
        try:
            key = base64.b64decode(raw)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"{name} 不是合法的 base64 密钥") from exc
        if len(key) != 32:
            raise ValueError(f"{name} 必须是 32 字节（base64 编码后 44 字符）")
        return key

    if settings.is_production:
        raise RuntimeError(f"生产环境必须显式配置 {name}")
    # dev 占位：从固定盐派生，稳定可复现（测试里加密→解密往返不受影响）
    return hashlib.sha256(f"dev-only-{name}".encode()).digest()


def _cipher(key: bytes) -> AESGCM:
    return AESGCM(key)


# ---------------------------------------------------------------------------
# 通用加密 / 解密（手机号与取件码共用同一套逻辑，仅密钥不同）
# ---------------------------------------------------------------------------
def encrypt_bytes(plaintext: str, *, key: bytes) -> bytes:
    """加密一段明文，返回 `v1:<nonce><ciphertext>` 字节串。"""
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = _cipher(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return _MAGIC + nonce + ciphertext


def decrypt_bytes(blob: bytes, *, key: bytes) -> str:
    """解密，返回明文字符串。密文被篡改 / 版本不匹配 → 抛 `ValueError`。"""
    if not blob.startswith(_MAGIC):
        raise ValueError("不支持的密文版本")
    nonce = blob[len(_MAGIC) : len(_MAGIC) + _NONCE_LEN]
    ciphertext = blob[len(_MAGIC) + _NONCE_LEN :]
    try:
        plaintext = _cipher(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:  # noqa: BLE001 - GCM 校验失败/密钥不对统一当"解不开"
        raise ValueError("密文校验失败，可能被篡改或密钥不匹配") from exc
    return plaintext.decode("utf-8")


# ---------------------------------------------------------------------------
# 取件码（FR-ORDER-10）
# ---------------------------------------------------------------------------
def encrypt_pick_code(plaintext: str) -> bytes:
    """取件码明文 → 密文。明文只在内存里短暂存在，**禁止落日志**。"""
    return encrypt_bytes(plaintext, key=_key_or_die(settings.pick_code_aes_key, name="PICK_CODE_AES_KEY"))


def decrypt_pick_code(blob: bytes) -> str:
    """取件码密文 → 明文。仅在「详情页解锁」时调用。"""
    return decrypt_bytes(blob, key=_key_or_die(settings.pick_code_aes_key, name="PICK_CODE_AES_KEY"))


# ---------------------------------------------------------------------------
# 手机号（D16 脱敏）
# ---------------------------------------------------------------------------
def encrypt_phone(plaintext: str) -> bytes:
    return encrypt_bytes(plaintext, key=_key_or_die(settings.phone_aes_key, name="PHONE_AES_KEY"))


def decrypt_phone(blob: bytes) -> str:
    return decrypt_bytes(blob, key=_key_or_die(settings.phone_aes_key, name="PHONE_AES_KEY"))


def mask_phone(phone: str) -> str:
    """手机号脱敏：`138****8888`（D16）。不依赖加密，纯展示用。"""
    if len(phone) != 11:
        return phone[:3] + "****" + phone[-4:]
    return f"{phone[:3]}****{phone[-4:]}"


__all__ = [
    "decrypt_bytes",
    "decrypt_phone",
    "decrypt_pick_code",
    "encrypt_bytes",
    "encrypt_phone",
    "encrypt_pick_code",
    "mask_phone",
]
