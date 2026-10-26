# CampusRun 后端镜像
# 构建上下文是 backend/，本文件在 deploy/，只用于 api / worker / beat 三个服务。
# 三者共用同一镜像，仅 command 不同——避免"API 能跑、worker 报 ImportError"。

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# curl 仅用于健康检查调试；tzdata 保证 Asia/Shanghai 生效（slim 镜像默认没有）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# 依赖先装（放前面，代码改动时才不会击穿这一层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
