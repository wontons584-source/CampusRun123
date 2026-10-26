"""FastAPI 应用入口。

T-01 的验收点：
    uvicorn app.main:app 可启动，GET /healthz 返回 {code:0,data:{db,redis}}。

T-04 的接线（本次新增）：
    `TraceMiddleware` + `ApiError` 异常处理器 + 兜底 500 处理器。

健康检查的语义（重要）：
- `/healthz` —— 存活探针。只要进程能响应就返回 200，组件状态放在 body 里。
  **不因为 DB 挂了就返回 5xx**，否则容器会被反复重启，问题从"依赖不可用"
  升级成"服务不可用"。
- `/readyz` —— 就绪探针。依赖不全时返回 503，让负载均衡把流量摘走。
两者分开是容器编排的标准做法，不要合并。

C1：本模块 import 期**不建立任何连接**。`create_engine` 是惰性的，
`configure_logging` 只碰 stdout。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.errors import ApiError, ErrCode
from app.core.logging import (
    TRACE_ID_HEADER,
    TraceMiddleware,
    configure_logging,
    get_logger,
    get_trace_id,
    request_trace_id,
)
from app.core.response import fail, ok
from app.db.session import engine, ping_db

logger = logging.getLogger("campusrun")


def _ping_redis() -> bool:
    """惰性导入 + 短超时：Redis 不可用时健康检查要快速返回，不能卡住探针。"""
    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5
        )
        return bool(client.ping())
    except Exception:  # noqa: BLE001
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201
    logger.info(
        "campusrun starting env=%s debug=%s", settings.app_env, settings.app_debug
    )
    if settings.is_production and "dev-only" in settings.jwt_secret_key:
        logger.error("生产环境仍在使用默认 JWT 密钥，请立即更换 JWT_SECRET_KEY")
    yield
    engine.dispose()
    logger.info("campusrun stopped")


def _register_error_handlers(app: FastAPI) -> None:
    """统一错误出口：所有非 2xx 响应都是 `{code,data,message,trace_id}`。"""

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError) -> JSONResponse:  # noqa: ANN202
        # 4xx 是用户错误，用 info；5xx 是我们自己的问题，用 error + 堆栈进日志
        trace_id = request_trace_id(request)
        log = get_logger("error")
        if exc.http_status >= 500:
            log.error(
                "api_error",
                code=exc.code,
                http_status=exc.http_status,
                message=exc.message,
                trace_id=trace_id,
            )
        else:
            log.info(
                "api_error",
                code=exc.code,
                http_status=exc.http_status,
                message=exc.message,
                trace_id=trace_id,
            )
        return JSONResponse(
            status_code=exc.http_status,
            content=fail(exc.code, exc.message, data=exc.data, trace_id=trace_id),
            headers={TRACE_ID_HEADER: trace_id},
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN202
        """兜底 500：**只回 trace_id，不回堆栈**。

        堆栈进日志（带 trace_id），响应里只留一句「系统繁忙」。
        把 `str(exc)` 回给客户端等于把表名、SQL、内网路径送给攻击者，
        而且用户也看不懂 —— 报障时给 trace_id 才是有效的沟通方式。

        注意：本处理器由 `ServerErrorMiddleware` 调度，它在 `TraceMiddleware`
        **之外**，所以必须用 `request_trace_id(request)`（读 `request.state`），
        不能用 `get_trace_id()` —— 那时 contextvar 已被还原成空串。
        """
        trace_id = request_trace_id(request)
        get_logger("error").exception(
            "unhandled_exception", exc_info=exc, trace_id=trace_id
        )
        return JSONResponse(
            status_code=500,
            content=fail(
                ErrCode.INTERNAL_ERROR,
                "系统繁忙，请稍后重试（如需协助请提供 trace_id）",
                trace_id=trace_id,
            ),
            headers={TRACE_ID_HEADER: trace_id},
        )


def create_app() -> FastAPI:
    # 日志必须先配好，否则第一条「启动中」日志不是 JSON（K-07）
    configure_logging(json_output=True)

    app = FastAPI(
        title="CampusRun API",
        version="0.1.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    _register_error_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[TRACE_ID_HEADER],
    )
    # add_middleware 是「后加的在最外层」：TraceMiddleware 最后加，
    # 保证 CORS 预检短路与异常处理器产生的响应也都带上 X-Trace-Id。
    app.add_middleware(TraceMiddleware)

    @app.get("/healthz", tags=["ops"], summary="存活探针")
    def healthz() -> JSONResponse:
        db_ok = ping_db()
        redis_ok = _ping_redis()
        body: dict[str, Any] = {
            "db": "ok" if db_ok else "down",
            "redis": "ok" if redis_ok else "down",
            "env": settings.app_env,
            "version": app.version,
        }
        return JSONResponse(status_code=200, content=ok(body))

    @app.get("/readyz", tags=["ops"], summary="就绪探针")
    def readyz() -> JSONResponse:
        db_ok = ping_db()
        redis_ok = _ping_redis()
        ready = db_ok and redis_ok
        # 依赖不全 → 503 + 50300（K-02），让负载均衡把流量摘走
        content = (
            ok({"db": db_ok, "redis": redis_ok}, "ready")
            if ready
            else fail(
                ErrCode.DEPENDENCY_UNAVAILABLE,
                "dependencies unavailable",
                data={"db": db_ok, "redis": redis_ok},
            )
        )
        return JSONResponse(status_code=200 if ready else 503, content=content)

    @app.get("/__trace_probe", tags=["ops"], include_in_schema=False)
    def _trace_probe() -> JSONResponse:
        """调试用：返回当前请求的 trace_id，验证中间件与响应头一致。

        仅用于验证 K-07，不承载业务；放在这里而不是单测里 mock，
        是因为中间件行为必须在真实 ASGI 栈上验证。
        """
        return JSONResponse(status_code=200, content=ok({"trace_id": get_trace_id()}))

    # ---- 业务路由挂载（T-13 起）----
    from app.api.v1 import orders as orders_api

    app.include_router(orders_api.router, prefix="/api/v1")

    return app


app = create_app()
