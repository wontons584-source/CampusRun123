# CampusRun · 校园跑腿系统

面向单校区在校学生的互助跑腿平台：发单人发布代取快递 / 代买 / 代送任务，系统按地理位置智能推送给附近处于接单模式的在校学生，学生在抢单大厅自主抢单，全流程可追溯、可评价、可治理。

> **当前状态：方案定稿，尚未开始编码。**
> 本仓库目前只包含文档层交付物，代码目录（`backend/` `web/` `admin/` `deploy/`）按 `docs/02-ARCHITECTURE.md` 第 11 章的任务列表逐项建立。

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

## 计划中的仓库结构

```
campus-run/
├── backend/            # FastAPI 应用（模块化单体，12 个 domain 包）
│   ├── app/
│   ├── alembic/        # 迁移脚本，必须提交
│   ├── scripts/        # 种子导入、压测、对账脚本
│   └── tests/
├── web/                # 用户端 React 18 + Vite
├── admin/              # 管理后台 React 18 + Vite
├── packages/           # web 与 admin 共享的 api-client / domains / ui-kit
├── deploy/             # Docker Compose、Dockerfile、Makefile、nginx.conf
└── docs/               # 本目录
```

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
