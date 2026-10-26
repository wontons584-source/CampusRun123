"""Redis 会话、键名规范与 Lua 装载器（T-05）。

## 三个设计决定

1. **`decode_responses=False`**：Lua 脚本里要处理 `bytes`（`ZADD` 的 member、
   二进制锁值），开成 `True` 会在脚本与客户端之间埋一层隐式编码差异。
   文本类读取由调用方显式 `.decode()`，比全局隐式解码更难出错。

2. **键名集中一处（K-13）**：业务代码里**禁止**手写 `f"grab:order:{oid}"`。
   键名散落的后果不是"不好看"，而是某天改一个前缀，扫不到全部引用点，
   于是出现"写 A 键、读 B 键"的静默失效 —— 这类 bug 不会报错，只会让功能变慢/失灵。
   所有键构造函数都在本模块，前缀常量也只在这里出现。

3. **Lua 用 `EVALSHA` + `NOSCRIPT` 自动重载**：每次 `EVAL` 都要把整段脚本
   传过网络（几十 KB），高频路径上这是纯浪费；`SCRIPT LOAD` 一次拿到 sha 之后
   只发 40 字节。但 Redis 重启/`SCRIPT FLUSH`/主从切换都会让 sha 失效，
   所以必须捕获 `NOSCRIPT`、重载一次、重试一次 —— 否则线上表现为
   "Redis 一重启，抢单接口全 500"。

**import 期零 I/O**（C1）：连接池惰性创建，Lua 源码在首次使用时才读文件。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Final

import redis
from redis.exceptions import NoScriptError

from app.core.config import settings
from app.core.errors import ApiError, ErrCode

# ---------------------------------------------------------------------------
# 键名规范（K-13）—— 唯一出处
# ---------------------------------------------------------------------------
#: 键前缀。改这里就是改全站，所以**只能有一份**。
PREFIX_GEO_RUNNERS: Final[str] = "geo:runners"
PREFIX_GEO_ORDERS: Final[str] = "geo:orders"
PREFIX_INFLIGHT: Final[str] = "runner:inflight"
PREFIX_ALIVE: Final[str] = "runner:alive"
PREFIX_GRAB_ORDER: Final[str] = "grab:order"
PREFIX_GRAB_USER: Final[str] = "grab:user"
PREFIX_IDEM: Final[str] = "idem"
PREFIX_PUSH_QUOTA: Final[str] = "pushq"
PREFIX_USER_FLAG: Final[str] = "u"
PREFIX_VERIFY: Final[str] = "vf"
PREFIX_BEAT_HEARTBEAT: Final[str] = "beat:hb"


def k_geo_runners(campus_id: int) -> str:
    """在岗接单人 GEO 集合（`GEOADD geo:runners:{campus}`）。"""
    return f"{PREFIX_GEO_RUNNERS}:{campus_id}"


def k_geo_orders(campus_id: int) -> str:
    """待接单池 GEO 集合（大厅距离排序用）。"""
    return f"{PREFIX_GEO_ORDERS}:{campus_id}"


def k_inflight(user_id: int) -> str:
    """接单人在途集合（Redis 只做召回期预过滤，DB 才是权威 —— BR-16/RSK-2）。"""
    return f"{PREFIX_INFLIGHT}:{user_id}"


def k_alive(user_id: int) -> str:
    """心跳存活标记，TTL 90s（BR-19）；扫描任务据此 `SREM` 僵尸成员。"""
    return f"{PREFIX_ALIVE}:{user_id}"


def k_grab_order(order_id: int) -> str:
    """订单抢单锁（同一单串行）。"""
    return f"{PREFIX_GRAB_ORDER}:{order_id}"


def k_grab_user(user_id: int) -> str:
    """用户抢单锁（同一用户串行，配合 advisory lock 防并发多抢）。"""
    return f"{PREFIX_GRAB_USER}:{user_id}"


def k_idem(user_id: int, idempotency_key: str) -> str:
    """幂等键（FR-GRAB-08），存首次成功响应，TTL 24h。"""
    return f"{PREFIX_IDEM}:{user_id}:{idempotency_key}"


def k_push_quota(user_id: int, window: str = "10m") -> str:
    """推送频控计数（BR-15：10 分钟内第 4 条降级为 HALL_ONLY）。"""
    return f"{PREFIX_PUSH_QUOTA}:{user_id}:{window}"


def k_user_flag(user_id: int, name: str = "flag") -> str:
    return f"{PREFIX_USER_FLAG}:{user_id}:{name}"


def k_token_version(user_id: int) -> str:
    """令牌版本缓存（ADR-05：`+1` 即旧 token 全失效）。"""
    return f"{PREFIX_USER_FLAG}:{user_id}:tv"


def k_verify(*parts: str | int) -> str:
    """认证频控键（IP / 学号 / 邮箱等多维）。"""
    return f"{PREFIX_VERIFY}:" + ":".join(str(p) for p in parts)


def k_beat_heartbeat(task: str) -> str:
    """beat 任务心跳（D21）：`beat:hb:silent_warn` EX 120，缺失即告警。"""
    return f"{PREFIX_BEAT_HEARTBEAT}:{task}"


# ---------------------------------------------------------------------------
# 连接池
# ---------------------------------------------------------------------------
_client: redis.Redis | None = None
_client_lock = threading.Lock()


def get_redis() -> redis.Redis:
    """进程内单例（惰性）。

    `health_check_interval` 让空闲连接被服务端断开后自动重连 —— 否则
    Redis 侧 `timeout` 一到，客户端拿着死连接一直报 `ConnectionResetError`。
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = redis.Redis.from_url(
                    settings.redis_url,
                    decode_responses=False,   # Lua 里要处理 bytes
                    socket_connect_timeout=1.0,
                    socket_timeout=1.0,
                    health_check_interval=30,
                    retry_on_timeout=True,
                )
    return _client


def set_redis(client: redis.Redis | None) -> None:
    """注入/重置单例（测试用）。传 `None` 表示下次 `get_redis()` 重新建。"""
    global _client
    with _client_lock:
        _client = client


def ping_redis() -> bool:
    """健康检查：只探活，不做业务命令。"""
    try:
        return bool(get_redis().ping())
    except Exception:  # noqa: BLE001 - 探针不该因任何异常把进程带崩
        return False


def reset_for_tests() -> None:
    """测试隔离用：丢弃单例（连接池里的连接由 GC 关闭）。"""
    set_redis(None)


# ---------------------------------------------------------------------------
# Lua 装载器
# ---------------------------------------------------------------------------
LUA_DIR: Final[Path] = Path(__file__).resolve().parent / "lua"


class LuaScript:
    """一个 Lua 脚本：源码惰性读取 + sha 缓存 + `NOSCRIPT` 自动重载。

    用法::

        ok = grab_lock(redis_client, order_id=1, token="t", ttl_ms=3000)

    内部走 `EVALSHA`；Redis 侧脚本缓存丢了会自动 `SCRIPT LOAD` 重试一次。
    """

    __slots__ = ("name", "_path", "_source", "_sha")

    def __init__(self, name: str, path: Path | None = None) -> None:
        self.name: str = name
        self._path: Path = path or (LUA_DIR / f"{name}.lua")
        self._source: str | None = None
        self._sha: str | None = None

    # ---------------- 源码与 sha ----------------
    @property
    def source(self) -> str:
        """脚本源码（首次访问时才读文件 —— import 期零 I/O）。"""
        if self._source is None:
            self._source = self._path.read_text(encoding="utf-8")
        return self._source

    @property
    def sha(self) -> str | None:
        return self._sha

    def load(self, client: redis.Redis) -> str:
        """`SCRIPT LOAD` 并缓存 sha。"""
        sha = client.script_load(self.source)
        self._sha = sha.decode() if isinstance(sha, bytes) else str(sha)
        return self._sha

    def ensure_loaded(self, client: redis.Redis) -> str:
        return self._sha or self.load(client)

    def forget(self) -> None:
        """丢掉缓存的 sha（测试里模拟"Redis 重启/SCRIPT FLUSH"）。"""
        self._sha = None

    # ---------------- 调用 ----------------
    def __call__(
        self,
        client: redis.Redis,
        keys: list[str] | tuple[str, ...] = (),
        args: list[Any] | tuple[Any, ...] = (),
    ) -> Any:
        """`EVALSHA` 调用；遇 `NOSCRIPT` 重载并**重试一次**。

        只重试一次是刻意的：如果重载后仍 `NOSCRIPT`，说明不是"缓存丢了"，
        而是脚本本身有问题（比如被 ACL 禁了 `SCRIPT`），继续重试只会放大故障。
        """
        sha = self.ensure_loaded(client)
        try:
            return client.evalsha(sha, len(keys), *keys, *args)
        except NoScriptError:
            sha = self.load(client)
            return client.evalsha(sha, len(keys), *keys, *args)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LuaScript {self.name} sha={self._sha}>"


#: 脚本注册表。**新增脚本只需在这里登记**，调用点按名字取，
#: 避免各处 `SCRIPT LOAD` 各写一遍（也就不会出现某一个脚本忘了预加载）。
grab_script: Final[LuaScript] = LuaScript("grab")
quota_script: Final[LuaScript] = LuaScript("quota")
presence_script: Final[LuaScript] = LuaScript("presence")

LUA_SCRIPTS: Final[dict[str, LuaScript]] = {
    "grab": grab_script,
    "quota": quota_script,
    "presence": presence_script,
}


def load_all_scripts(client: redis.Redis | None = None) -> dict[str, str]:
    """启动时预加载全部脚本，返回 `{name: sha}`。

    建议在应用 lifespan 里调用（失败只告警不阻断启动：Redis 不可用时
    服务应降级为"仅大厅"而不是起不来 —— FR-PUSH-06 / RSK-3）。
    """
    target = client or get_redis()
    return {name: script.load(target) for name, script in LUA_SCRIPTS.items()}


# ---------------------------------------------------------------------------
# 三个"能装载、能执行"的最小封装（业务语义留给 T-15/T-18/T-20）
# ---------------------------------------------------------------------------
def grab_lock(client: redis.Redis, *, order_id: int, token: str, ttl_ms: int = 3000) -> bool:
    """订单抢单锁：`SET grab:order:{oid} {token} NX PX {ttl}`。

    返回 `True` 表示拿到锁。释放必须校验 token（Lua 里做），
    否则会误删别人刚抢到的锁 —— 那正是 T-20 要做的事。
    """
    result = grab_script(client, [k_grab_order(order_id)], [token, ttl_ms])
    return int(result) == 1


def bump_quota(client: redis.Redis, *, user_id: int, kind: str = "10m", ttl_s: int = 600) -> int:
    """频控计数：`INCR` 后首次设置 `EXPIRE`，返回当前计数。

    `INCR` + `EXPIRE` 分两条命令会在中间崩溃时留下"永不过期的计数器"，
    导致该用户被永久限流 —— 所以必须 Lua 原子执行。
    """
    return int(quota_script(client, [k_push_quota(user_id, kind)], [ttl_s]))


def heartbeat(
    client: redis.Redis, *, campus_id: int, user_id: int, lng: float, lat: float, ttl_s: int = 90
) -> bool:
    """心跳：`GEOADD` 进在岗集合 + `EXPIRE` 整个集合 + 写存活标记（BR-19 的 TTL 兜底）。"""
    presence_script(
        client,
        [k_geo_runners(campus_id), k_alive(user_id)],
        [lng, lat, user_id, ttl_s],
    )
    return True


def dependency_down(exc: Exception | None = None) -> ApiError:
    """Redis 不可用时统一抛 `503/50300`。

    **不要**在调用点自己 `except: pass` —— 那会把"缓存挂了"变成
    "看起来一切正常但召回是空的"，排查成本极高。降级决策留给业务层
    （FR-PUSH-06：Redis 挂 → 降级"仅大厅"），本函数只负责给出正确的错误码。
    `exc` 仅用于日志关联，不进响应体（避免泄露内网地址）。
    """
    _ = exc
    return ApiError(
        503, ErrCode.DEPENDENCY_UNAVAILABLE, "缓存服务暂时不可用，请稍后重试"
    )


__all__ = [
    "LUA_DIR",
    "LUA_SCRIPTS",
    "LuaScript",
    "bump_quota",
    "dependency_down",
    "get_redis",
    "grab_lock",
    "grab_script",
    "heartbeat",
    "k_alive",
    "k_beat_heartbeat",
    "k_geo_orders",
    "k_geo_runners",
    "k_grab_order",
    "k_grab_user",
    "k_idem",
    "k_inflight",
    "k_push_quota",
    "k_token_version",
    "k_user_flag",
    "k_verify",
    "load_all_scripts",
    "ping_redis",
    "presence_script",
    "quota_script",
    "reset_for_tests",
    "set_redis",
]
