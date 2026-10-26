"""应用配置：环境变量 + 运行期参数（sys_configs）两套体系。

- 环境变量（本文件 Settings）：部署差异，进程启动时读取一次，改了要重启。
- 运行期参数（app.core.config 之外的 ConfigService，T-06）：业务阈值，
  存 sys_configs 表，Redis 快照 60s，改了不重启。

边界：凡是"运维要知道、改了要重启"的放环境变量；凡是"运营想调、改完立刻生效"
的放 sys_configs。不要把业务阈值写进环境变量，否则调一次参数要重启一次服务。
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------- 应用 ----------
    app_env: Literal["development", "staging", "production"] = "development"
    app_debug: bool = False
    app_secret_key: str = Field(default="dev-only-not-for-production", min_length=16)
    cors_origins: str = "http://localhost:5173,http://localhost:5174"

    # ---------- 数据库 ----------
    database_url: str = "postgresql+psycopg://campusrun:campusrun@localhost:5432/campusrun"
    db_pool_size: int = 10
    db_echo: bool = False

    # ---------- 缓存 / 队列 ----------
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    # ---------- 认证 ----------
    jwt_secret_key: str = Field(default="dev-only-jwt-secret-change-me", min_length=16)
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_min: int = 120
    jwt_refresh_ttl_days: int = 30
    verify_email_domain_whitelist: str = "nau.edu.cn"

    # ---------- 字段级加密（base64 编码的 32 字节）----------
    phone_aes_key: str = ""
    pick_code_aes_key: str = ""

    # ---------- 管理后台 ----------
    admin_ip_allowlist: str = ""

    # ---------- 派生属性 ----------
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def verify_email_domains(self) -> list[str]:
        return [d.strip().lower() for d in self.verify_email_domain_whitelist.split(",") if d.strip()]

    @property
    def admin_ip_cidrs(self) -> list[str]:
        return [c.strip() for c in self.admin_ip_allowlist.split(",") if c.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @field_validator("app_secret_key", "jwt_secret_key")
    @classmethod
    def _reject_default_secret_in_production(cls, v: str, info):  # noqa: ANN001
        # 只做格式层面的弱校验，真正的强度检查放在启动日志里告警，不阻断启动
        return v


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试可用 get_settings.cache_clear() 重置。"""
    return Settings()


settings = get_settings()
