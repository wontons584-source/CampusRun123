# CampusRun · 校园跑腿系统

面向单校区在校学生的互助跑腿平台：发单人发布代取快递 / 代买 / 代送任务，系统按地理位置智能推送给附近处于接单模式的在校学生，学生在抢单大厅自主抢单，全流程可追溯、可评价、可治理。

> **当前状态：地基已完成，最小可演示链路开发中。**
>
> 已完成并在**真实 PostgreSQL 16 + Redis 7** 上验证：14 张表与 27 条索引的完整迁移、
> 17 条订单状态迁移的状态机内核、真实 SQLAlchemy 仓储（CAS 乐观锁 + 并发 seq 分配）、
> Redis Lua 脚本装载、参数化配置热生效、发单接口（取件码 AES-GCM 加密落库）。
>
> 测试规模：**707 个用例通过（615 单元 + 92 集成）**，集成测试跑在真实 PostgreSQL 16 与 Redis 7 上。
>
> 未开始：学号认证、推送调度、评价与纠纷仲裁、管理后台。

---

## 快速开始（本地）

需要 Docker Desktop 与 Python 3.11+。

```bash
# 1. 起依赖（PostgreSQL + Redis）
cd deploy && docker compose up -d postgres redis

# 2. 装依赖
cd ../backend
python -m pip install -r requirements.txt

# 3. 建表 + 灌参数
export DATABASE_URL="postgresql+psycopg://campusrun:campusrun@localhost:5432/campusrun"
export REDIS_URL="redis://localhost:6379/0"
python -m alembic upgrade head

# 4. 起服务
uvicorn app.main:app --reload
```

打开 <http://localhost:8000/docs> 可直接调接口。

### 跑测试

```bash
python -m pytest tests                              # 单元测试（不需要数据库）
TEST_DATABASE_URL=$DATABASE_URL \
  python -m pytest tests -m integration             # 集成测试（需要真库）
```

> 集成测试的每个用例跑在独立事务里、结束回滚，可反复执行不脏库。

---

## 当前实现进度

| 模块 | 状态 | 说明 |
|---|---|---|
| 数据模型与迁移 | ✅ 完成 | 14 表 / 27 索引（5 条部分索引）/ 3 个迁移 |
| 通用内核 | ✅ 完成 | 30+ 业务错误码、统一响应信封、游标分页、可注入时钟、trace 链路 |
| 订单状态机 | ✅ 完成 | 17 条迁移 + 19 个动作 + 守卫纯函数 |
| 真实仓储 | ✅ 完成 | CAS 乐观锁、`seq` 并发分配、约束冲突映射业务码 |
| Redis / Lua | ✅ 完成 | 抢单锁、频控、GEO 心跳，`NOSCRIPT` 自动重载 |
| 参数化配置 | ✅ 完成 | `sys_configs` + Redis 快照 60s 热生效 |
| 发单 | ✅ 完成 | 取件码加密、酬劳区间、发单窗、待接单上限 |
| 认证 | ✅ 完成 | 手机号 + 密码（bcrypt）、JWT 签发与校验 |
| 大厅 / 抢单 / 履约 / 确认 | ✅ 完成 | Redis 锁预过滤 + 状态机 CAS 定胜负；跳级由守卫拦截 |
| 前端页面 | ✅ 完成 | 登录 / 大厅 / 发单 / 订单详情（4 页，React 18 + Vite） |
| POI 字典 | ✅ 完成 | 8 条南审地点 + 幂等种子脚本 |
| 学号认证 / 推送 / 评价 / 纠纷 / 后台 | ⬜ 未开始 | 见 `docs/02-ARCHITECTURE.md` 第 11 章 |

---

## 文档索引

| 文档 | 内容 | 主要读者 |
|---|---|---|
| [`docs/01-PRD.md`](docs/01-PRD.md) | 产品需求定稿：目标与场景、73 条 FR、35 条业务规则、17 条状态迁移、22 条验收标准 | 产品 / 开发 / 验收 |
| [`docs/02-ARCHITECTURE.md`](docs/02-ARCHITECTURE.md) | 架构设计与任务分解：8 条 ADR、技术选型、数据模型、接口清单、复杂度分析、M1 的 35 个任务 | 开发 |
| [`docs/class-diagram.mermaid`](docs/class-diagram.mermaid) | 领域类图与服务类关系 | 开发 |
| [`docs/sequence-diagram.mermaid`](docs/sequence-diagram.mermaid) | 发单→召回→推送→抢单→履约→确认→评价全链路时序，含异常分支 | 开发 |
| [`docs/archive/00-PRD-v0.1-draft.md`](docs/archive/00-PRD-v0.1-draft.md) | v0.1 技术向草稿，已被 01-PRD 取代，**仅作历史参考，不要按它实现** | 存档 |

阅读顺序：先 `01-PRD.md` 第 1 章（三个产品判断）、第 6 章（MVP 边界）；再看 `02-ARCHITECTURE.md` 第 1 章（ADR）与第 11 章（任务列表）。

---

## 仓库结构

```
campus-run/
├── backend/            # FastAPI 应用（模块化单体）
│   ├── app/
│   │   ├── core/       # 错误码、响应、分页、时钟、日志、加密
│   │   ├── db/         # 会话、Redis、Lua 脚本
│   │   ├── models/     # 14 张表的 ORM 定义
│   │   ├── modules/    # 领域模块（order / admin / auth …）
│   │   └── api/        # HTTP 路由
│   ├── alembic/        # 迁移脚本，必须提交
│   ├── scripts/        # 种子导入、压测、对账脚本
│   └── tests/
├── web/                # 用户端 React 18 + Vite
├── deploy/             # Docker Compose、Dockerfile、Makefile、nginx.conf
└── docs/               # 设计与验收依据
```

> `admin/`（管理后台）与 `packages/`（共享包）尚未创建，见架构文档第 11 章。

---

## 开发约定

### 分支

| 分支 | 用途 |
|---|---|
| `main` | 随时可部署的稳定分支，禁止直接提交 |
| `feat/<简短描述>` | 功能开发，从 `main` 切出，合并前需自测通过 |
| `fix/<简短描述>` | 缺陷修复 |

### 提交信息

统一格式 `type: 描述`，描述用中文、动词开头、不超过 50 字，不加句号。

| type | 用途 |
|---|---|
| `feat` | 新功能 |
| `fix` | 缺陷修复 |
| `docs` | 文档 |
| `refactor` | 重构（不改变外部行为） |
| `test` | 测试 |
| `chore` | 构建、依赖、配置、脚手架 |
| `perf` | 性能优化 |

示例：

```
feat: 实现抢单接口的幂等与在途上限校验
fix: 修正回滚到待确认后立即被自动确认的问题
docs: 补充 BR-33 失联判定的分档说明
chore: 升级 SQLAlchemy 到 2.0.36
```

### 提交粒度

一个提交只做一件事。文档改动与代码改动分开提交；数据库迁移与使用它的代码放在同一个提交里，避免出现「代码已上线、迁移没跑」的中间状态。

### 数据库变更

**必须新增 Alembic 迁移**，禁止直接改生产表结构。`backend/alembic/versions/` 下的文件全部纳入版本控制（`.gitignore` 中不做任何忽略）。

---

## 安全约定

以下内容**永不提交**，已在 `.gitignore` 中排除：

- 任何 `.env` 文件（仅 `!.env.example` 模板留在仓库里）
- 证书与私钥：`*.pem` `*.key` `*.p12`
- `secrets/` 目录
- 真实手机号、学号、取件码的测试样本

新增环境变量时，只改 `.env.example` 并写明用途，不要写真实值。代码中通过 `app/core/config.py` 读取，禁止散落 `os.getenv`。

---

## 上线前必须先确认的三件事

这三项在 `docs/01-PRD.md` 第 11 章标为 🔴 阻塞项，任何一项不成立都会改变 M1 范围：

1. 资金是否锁定**线下结算**（平台不经手钱），以及是否禁发 > 20 元的大额单。
2. 小程序是否有**企业主体**可用（个人主体通常开不了「跑腿代购」生活服务类目，验收项 D13–D15 已标注 M4 生效）。
3. 学校 `@nau.edu.cn` 邮箱能否收到**外部系统发送的验证码**——这一条一条命令就能验证，但决定认证模块是否整个换方案。
