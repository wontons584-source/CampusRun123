"""结构化日志与 trace_id 透传（T-04, K-07）。

两件事必须同时成立，否则线上排查等于盲人摸象：
1. **日志是 JSON 一行一条**（structlog），可以直接进 Loki/ES，不需要正则解析；
2. **trace_id 贯穿全链路**：请求头缺失则新建，存在则透传（前端/网关可串联），
   写在响应头 `X-Trace-Id` 里，用户报障时截图就能定位。

K-07 红线（不是「打码」而是「不出现」）：
- 手机号、学号：**禁止**完整打印（`phone_mask` 可以，`phone_enc` 不行）；
- **取件码属于「禁止出现」级别**：明文只在授权 DTO 中短暂存在，
  日志、错误信息、审计的 `before/after` 里都不许出现。
因此本模块提供 `scrub()` 作为最后一道闸：任何 dict 进日志前过一遍。

注意：`app.core.logging` 与标准库 `logging` 同名。本文件内 `import logging`
拿到的是标准库（绝对导入优先），不会自我引用。
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from contextvars import ContextVar, Token
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# ---------------------------------------------------------------------------
# trace_id contextvar：唯一来源。response.py 从这里取，middleware 在这里写。
# ---------------------------------------------------------------------------
TRACE_ID_HEADER: str = "X-Trace-Id"

_trace_id: ContextVar[str] = ContextVar("trace_id", default="")

# 敏感字段名（小写比较）：值一律替换为掩码，绝不打印
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "phone",
        "phone_enc",
        "phone_plain",
        "student_no",
        "student_no_input",
        "pick_code",
        "pick_code_enc",
        "pick_code_plain",
        "password",
        "access_token",
        "refresh_token",
        "authorization",
        "code",  # 验证码
    }
)


def new_trace_id() -> str:
    """新建 trace_id：32 位 hex（够短可贴群、够长不撞）。"""
    return uuid.uuid4().hex


def get_trace_id() -> str:
    """取当前上下文 trace_id；不在请求上下文时返回空串（不是抛错）。"""
    return _trace_id.get()


def set_trace_id(trace_id: str) -> Token[str]:
    """写入 trace_id，返回 token 供 `reset_trace_id` 还原（must reset，防串上下文）。"""
    return _trace_id.set(trace_id)


def reset_trace_id(token: Token[str]) -> None:
    _trace_id.reset(token)


def request_trace_id(request: Request) -> str:
    """取某个请求的 trace_id，**优先从 `request.state` 读**。

    为什么不能只依赖 contextvar：Starlette 的 `ServerErrorMiddleware` 在
    **用户中间件之外**，所以「未捕获异常」的兜底处理器运行时，
    `TraceMiddleware` 的 `finally` 已经把 contextvar 还原了 —— 此时
    `get_trace_id()` 是空串，500 响应就会丢掉 trace_id，而这恰恰是最需要它的场景。
    `request.state` 挂在 `scope["state"]` 上，贯穿整条中间件链，能稳定读到。
    """
    from_state = getattr(request.state, "trace_id", "")
    if from_state:
        return str(from_state)
    return get_trace_id() or new_trace_id()


def scrub(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """脱敏：敏感字段的值替换为 `***`（保留键名，便于确认"字段确实存在"）。

    只处理一层 dict（日志上下文都是扁平结构）；嵌套 dict 递归处理，
    list 内的 dict 也处理 —— 事件流里 `images` 是 list，`payload` 里会有嵌套。
    """
    if payload is None:
        return {}
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key.lower() in _SENSITIVE_KEYS:
            out[key] = "***"
        elif isinstance(value, Mapping):
            out[key] = scrub(value)
        elif isinstance(value, list | tuple):
            out[key] = [scrub(v) if isinstance(v, Mapping) else v for v in value]
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# structlog 配置
# ---------------------------------------------------------------------------
def configure_logging(level: int = logging.INFO, *, json_output: bool = True) -> None:
    """配置 structlog：JSON 输出 + contextvars 合并（trace_id 自动带上）。

    **不要**在 import 期调用它（C1）；由 `create_app()` 或 worker 启动时调。
    重复调用是安全的（幂等覆盖配置）。
    """

    def _add_trace_id(
        _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        trace_id = get_trace_id()
        if trace_id:
            event_dict.setdefault("trace_id", trace_id)
        return event_dict

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_trace_id,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # 标准库 logging 也走同一出口，避免 uvicorn/sqlalchemy 的日志变成非 JSON
    stdlib_handler = logging.StreamHandler(stream=sys.stdout)
    stdlib_handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [stdlib_handler]
    root.setLevel(level)


def get_logger(name: str | None = None) -> Any:
    """取 structlog logger。首个绑定字段固定 `logger=<name>`，便于按模块过滤。"""
    log = structlog.get_logger()
    if name:
        return log.bind(logger=name)
    return log


# ---------------------------------------------------------------------------
# TraceMiddleware
# ---------------------------------------------------------------------------
class TraceMiddleware(BaseHTTPMiddleware):
    """trace_id 生成/透传 + 访问日志 + 响应头回写。

    顺序要求（main.py 里的 add_middleware 顺序是**反序生效**）：
    TraceMiddleware 必须**最后** add，这样它最外层先执行，
    保证 CORS 预检、异常处理器产生的响应也能带上 `X-Trace-Id`。

    为什么不直接在路由里生成：trace_id 要覆盖 404/500/中间件短路，
    路由层做不到。
    """

    def __init__(self, app: Any, *, header_name: str = TRACE_ID_HEADER) -> None:
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(self.header_name, "")
        # 透传规则：外部给的值得可信吗？长度与字符集做一次白名单校验，
        # 防止有人塞超长串污染日志/响应头（日志注入）。
        trace_id = incoming if _is_valid_trace_id(incoming) else new_trace_id()
        # 同时写 contextvar（日志用）与 request.state（异常处理器用，见 request_trace_id）
        request.state.trace_id = trace_id
        token = set_trace_id(trace_id)
        structlog.contextvars.bind_contextvars(trace_id=trace_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            cost_ms = round((time.perf_counter() - started) * 1000, 2)
            response.headers[self.header_name] = trace_id
            get_logger("http").info(
                "request",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                cost_ms=cost_ms,
            )
            return response
        except Exception:
            cost_ms = round((time.perf_counter() - started) * 1000, 2)
            get_logger("http").exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                cost_ms=cost_ms,
            )
            raise
        finally:
            structlog.contextvars.clear_contextvars()
            reset_trace_id(token)


def _is_valid_trace_id(value: str) -> bool:
    """透传白名单：8–64 位且只含 URL 安全字符。"""
    if not 8 <= len(value) <= 64:
        return False
    return all(c.isalnum() or c in "-_." for c in value)
