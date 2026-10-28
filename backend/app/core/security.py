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
import datetime
import hashlib
import os
from typing import Any, Final

import bcrypt
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings
from app.core.errors import ApiError, ErrCode

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


def phone_digest(phone: str) -> str:
    """手机号**确定性**摘要（64 位十六进制），用作 `auth_identities.provider_uid`。

    ## 为什么不是直接用 `phone_enc` 查登录

    `phone_enc` 是 AES-GCM 密文，每次加密都随机 nonce ⇒ **同一手机号两次加密的
    密文不同**，根本没法 `WHERE phone_enc = ?` 查用户（也正因如此它才能直接当
    "密文即脱敏"用）。要按手机号登录，需要一个**确定性**的索引值。

    ## 为什么用摘要而不是"解密全表再比对"

    后者是 O(n) 次 AES-GCM 解密 + 全表扫，用户量一上来就是事故；而且它要求把
    每一行的密文都解出来 —— 一次 SQL 注入就能顺走全部明文手机号。

    ## 为什么 sha256 不加盐

    这一列是**索引键**，必须确定性；加盐就失去查询能力。它的隐私保护不是靠盐，
    而是靠：① 明文手机号本身不落库；② 该列只在服务端内部做等值匹配，不进任何 DTO。
    手机号空间是 10^11 量级、且运营商号段有限，摘要**不是**加密 —— 真正的防线是
    `phone_enc`（AES-GCM）与"明文禁止出库"的纪律。这里只是把"能不能查"这件事做对。

    Args:
        phone: 11 位手机号明文。

    Returns:
        64 字符十六进制摘要，长度与 `auth_identities.provider_uid varchar(64)` 一致。
    """
    return hashlib.sha256(phone.strip().encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 密码哈希（bcrypt）
# ---------------------------------------------------------------------------
#: bcrypt 的输入上限：算法本身只处理前 72 **字节**，超出部分被静默丢弃。
#: bcrypt>=4.1 的 `hashpw` 对超长输入直接抛 `ValueError`（而不是静默截断），
#: 所以必须由我们显式截断 —— 否则一个 100 字符的密码会在 500 而不是 400 上炸。
_BCRYPT_MAX_BYTES: Final[int] = 72

#: 代价因子。12 ≈ 250ms/次（现代 x86），是"登录不快、爆破更慢"的常用折中；
#: 每 +1 耗时翻倍，改这里要同步评估登录接口的 P99。
_BCRYPT_ROUNDS: Final[int] = 12


def hash_password(password: str) -> str:
    """明文密码 → bcrypt 哈希（自带随机盐，形如 `$2b$12$...`，60 字符）。

    **不要**自己拼 salt：bcrypt 的盐必须由 `gensalt()` 生成（CSPRNG），
    手写的 `uuid4()` / `random` 会把整批哈希降到可彩虹表规模。

    Args:
        password: 用户输入的明文密码（按 UTF-8 取前 72 字节参与运算）。

    Returns:
        ASCII 哈希字符串，可直接存 `users.password_hash varchar(128)`。

    Raises:
        ApiError: 密码为空（400/40009）。空密码不该走到哈希这一步。
    """
    if not password:
        raise ApiError(400, ErrCode.INVALID_ARGUMENT, "密码不能为空")
    raw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    """校验明文密码与哈希是否匹配。

    ## 三个失败路径都必须返回 `False` 而不是抛错

    - `password_hash is None`（未设置密码的账号，如仅微信登录）；
    - 哈希格式非法（历史脏数据、被截断的列值）；
    - 密码不匹配。

    对调用方（登录接口）而言三者是**同一件事**："这组凭据无效"。
    区分它们会把"哪个手机号存在但没设密码"泄露给攻击者。

    Args:
        password: 用户输入的明文密码。
        password_hash: 库里存的 bcrypt 哈希，可为 `None`。

    Returns:
        匹配为 `True`，其余一律 `False`。
    """
    if not password_hash:
        return False
    try:
        raw = password.encode("utf-8")[:_BCRYPT_MAX_BYTES]
        return bcrypt.checkpw(raw, password_hash.encode("ascii"))
    except (ValueError, TypeError, UnicodeError):
        # 哈希串损坏 / 不是 bcrypt 格式：当作"不匹配"，不把内部状态抛给用户
        return False


# ---------------------------------------------------------------------------
# 访问令牌（JWT，HS256）
# ---------------------------------------------------------------------------
#: 载荷里的固定键名（前端不解析它，只有服务端用；但改名等于全量登出，慎改）。
JWT_SUB: Final[str] = "sub"          # 用户 id（字符串，PyJWT 要求 str）
JWT_TV: Final[str] = "tv"            # token_version（ADR-05 即时失效）
JWT_IAT: Final[str] = "iat"
JWT_EXP: Final[str] = "exp"


def create_access_token(
    user_id: int,
    *,
    token_version: int = 0,
    now: datetime.datetime | None = None,
    ttl_minutes: int | None = None,
) -> str:
    """签发访问令牌（HS256）。

    ## 为什么载荷里要带 `tv`（token_version）

    JWT 是无状态的：签发之后服务端无法"收回"。改密码、封号、注销设备都需要
    **立即**让旧令牌失效，而等 `exp` 到期的窗口可能有 2 小时。
    `tv` 与 `users.token_version` 比对一次就能做到"服务端 +1 ⇒ 全部旧令牌立刻失效"
    （ADR-05），代价是每次鉴权多一次按主键点查（本来就要查用户做状态校验）。

    ## 为什么默认取**真实系统时钟**而不是业务 `Clock`

    `app.core.clock.clock` 是可冻结的业务时钟（测试注入 45 分钟边界、演示脚本
    注入发单窗内时刻）。令牌的有效期是**安全属性**，必须跟真实世界的时间走：
    一旦业务时钟被冻结到一个过去或未来的时刻，用它的 `exp` 会让**所有令牌瞬间
    过期**（或永不过期），而 `decode_token`（PyJWT 内部用系统时间）也不会认同
    那个时刻。两者的时间轴必须一致，所以这里默认系统时钟，`now` 仅用于测试。

    Args:
        user_id: 用户主键。
        token_version: 当前 `users.token_version`，默认 0。
        now: 签发时刻（测试注入；`None` = 系统 UTC 时钟）。
        ttl_minutes: 有效期（分钟），默认 `settings.jwt_access_ttl_min`。

    Returns:
        `xxx.yyy.zzz` 形式的紧凑 JWT。
    """
    moment = now if now is not None else datetime.datetime.now(tz=datetime.UTC)
    if moment.tzinfo is None:                     # naive 视为 UTC（K-04 的兜底口径）
        moment = moment.replace(tzinfo=datetime.UTC)
    ttl = settings.jwt_access_ttl_min if ttl_minutes is None else ttl_minutes
    payload: dict[str, Any] = {
        JWT_SUB: str(user_id),
        JWT_TV: int(token_version),
        JWT_IAT: moment,
        JWT_EXP: moment + datetime.timedelta(minutes=ttl),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """校验并解析令牌，返回载荷。

    两条错误必须分开（前端行为不同）：
    - `exp` 过期 → `401/40102`：客户端应静默刷新或重新登录；
    - 签名错 / 格式错 → `401/40103`：令牌是伪造或损坏的，直接清掉本地存储。

    合成一个码的话，前端只能一律"登出"，用户会在令牌自然过期时被踢出去。

    Args:
        token: 不含 `Bearer ` 前缀的裸 JWT。

    Returns:
        载荷字典（至少含 `sub` / `tv`）。

    Raises:
        ApiError: 401/40102（过期）或 401/40103（非法）。
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ApiError(401, ErrCode.TOKEN_EXPIRED, "登录已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise ApiError(401, ErrCode.TOKEN_INVALID, "登录状态无效，请重新登录") from exc
    return payload


def token_subject(payload: dict[str, Any]) -> int:
    """从载荷取用户 id（`sub` 是字符串，这里统一转 int）。

    `sub` 不是数字 → `401/40103`，而不是 `ValueError`/500：这是令牌被伪造的特征。
    """
    raw = payload.get(JWT_SUB)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError(401, ErrCode.TOKEN_INVALID, "登录状态无效，请重新登录") from exc


def token_version_of(payload: dict[str, Any]) -> int:
    """从载荷取 `tv`；缺失按 0 处理（兼容早期未带 `tv` 的令牌）。"""
    raw = payload.get(JWT_TV, 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def bearer_token(authorization: str | None) -> str:
    """从 `Authorization: Bearer <token>` 头里取出裸令牌。

    Args:
        authorization: 原始请求头值，可为 `None`（未登录）。

    Returns:
        裸 JWT 字符串。

    Raises:
        ApiError: 401/40101（没带凭据）或 401/40103（方案不是 Bearer / 空令牌）。
    """
    if not authorization or not authorization.strip():
        raise ApiError(401, ErrCode.UNAUTHENTICATED, "请先登录")
    scheme, _, credentials = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        raise ApiError(401, ErrCode.TOKEN_INVALID, "登录状态无效，请重新登录")
    return credentials.strip()


__all__ = [
    "bearer_token",
    "create_access_token",
    "decode_token",
    "decrypt_bytes",
    "decrypt_phone",
    "decrypt_pick_code",
    "encrypt_bytes",
    "encrypt_phone",
    "encrypt_pick_code",
    "hash_password",
    "mask_phone",
    "phone_digest",
    "token_subject",
    "token_version_of",
    "verify_password",
]
