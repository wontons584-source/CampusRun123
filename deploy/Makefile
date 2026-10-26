# CampusRun 本地开发命令入口
# 用法：make help

SHELL := /bin/sh
COMPOSE := docker compose
BACKEND := ../backend

.DEFAULT_GOAL := help

.PHONY: help up down restart logs ps migrate revision seed test lint fmt verify clean

help: ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## 启动全部服务（postgres/redis/api/worker/beat/nginx）
	$(COMPOSE) up -d --build
	@echo "等待健康检查…"
	@$(COMPOSE) ps

down: ## 停止并移除容器（保留数据卷）
	$(COMPOSE) down

restart: ## 重启 api / worker / beat（改代码后用）
	$(COMPOSE) restart api worker beat

logs: ## 跟踪 api 日志
	$(COMPOSE) logs -f --tail=100 api

ps: ## 查看服务状态
	$(COMPOSE) ps

migrate: ## 执行数据库迁移到最新
	$(COMPOSE) exec api alembic upgrade head

revision: ## 新建迁移，用法 make revision m="add xxx"
	@test -n "$(m)" || (echo "用法: make revision m=\"描述\"" && exit 1)
	$(COMPOSE) exec api alembic revision --autogenerate -m "$(m)"

seed: ## 导入 POI 种子数据（需先 migrate）
	$(COMPOSE) exec api python scripts/seed_pois.py data/pois_nau.csv

test: ## 跑单元测试（不需要数据库）
	$(COMPOSE) exec api pytest -m "not integration and not concurrent"

test-all: ## 跑全部测试（含集成与并发）
	$(COMPOSE) exec api pytest

test-integration: ## 跑真库集成测试（连 compose 里的 postgres）
	$(COMPOSE) exec -e TEST_DATABASE_URL=$$DATABASE_URL api pytest -m integration

lint: ## 静态检查
	$(COMPOSE) exec api ruff check app tests
	$(COMPOSE) exec api ruff format --check app tests

fmt: ## 自动格式化
	$(COMPOSE) exec api ruff format app tests
	$(COMPOSE) exec api ruff check --fix app tests

verify: ## M0 验收：容器健康 + 迁移无差异 + 单元测试
	@echo "== 1/3 容器健康 =="
	@$(COMPOSE) ps --format '{{.Name}}\t{{.Status}}' | grep -v "healthy" || true
	@echo "== 2/3 迁移无漂移 =="
	@$(COMPOSE) exec api alembic check
	@echo "== 3/3 单元测试 =="
	@$(MAKE) test

clean: ## 清空数据卷（危险：会删掉本地数据库）
	@printf "将删除本地 pgdata/redisdata，确认？[y/N] " && read ans && [ "$$ans" = "y" ]
	$(COMPOSE) down -v
