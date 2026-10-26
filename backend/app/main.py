"""FastAPI 应用入口。

T-01 的验收点：
    uvicorn app.main:app 可启动，GET /healthz 返回 {code:0,data:{db,redis}}。

健康检查的语义（重要）：
- `/healthz` —— 存活探针。只要进程能响应就返回 200，组件状态放在 body 里。
  **不因为 DB 挂了就返回 5xx**，否则容器会被反复重启，问题从"依赖不可用"
  升级成"服务不可用"。
- `/readyz` —— 就绪探针。依赖不全时返回 503，让负载均衡把流量摘走。
两者分开是容器编排的标准做法，不要合并。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
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


def create_app() -> FastAPI:
    app = FastAPI(
        title="CampusRun API",
        version="0.1.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id"],
    )

    @app.get("/healthz", tags=["ops"], summary="存活探针")
    def healthz() -> JSONResponse:
        db_ok = ping_db()
        redis_ok = _ping_redis()
        return JSONResponse(
            status_code=200,
            content={
                "code": 0,
                "data": {
                    "db": "ok" if db_ok else "down",
                    "redis": "ok" if redis_ok else "down",
                    "env": settings.app_env,
                    "version": app.version,
                },
                "message": "ok",
            },
        )

    @app.get("/readyz", tags=["ops"], summary="就绪探针")
    def readyz() -> JSONResponse:
        db_ok = ping_db()
        redis_ok = _ping_redis()
        ready = db_ok and redis_ok
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "code": 0 if ready else 50300,
                "data": {"db": db_ok, "redis": redis_ok},
                "message": "ready" if ready else "dependencies unavailable",
            },
        )

    return app


app = create_app()
