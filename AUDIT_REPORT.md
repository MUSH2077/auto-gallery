# auto-gallery 稳定性审计报告

**审计日期**: 2026-06-23  
**审计分支**: `gitlike-gallery`  
**审计范围**: 全项目（只读，未修改任何文件）

---

## 1. 项目事实

### 1.1 技术栈

| 层级 | 技术 | 版本 |
|------|------|------|
| 后端框架 | FastAPI (Python 3.12) | 0.115.6 |
| ORM | SQLAlchemy 2.0 (async) | 2.0.36 |
| ASGI 服务器 | Uvicorn | 0.34.0 |
| 数据库 | PostgreSQL 16 (Alpine) | — |
| 缓存/队列 | Redis 7 + RQ | 7-alpine / 2.1.0 |
| 搜索引擎 | Meilisearch | v1.12 |
| 下载工具 | gallery-dl | 1.32.0 |
| 图片处理 | pyvips + Pillow | 2.2.3 / 10.4.0 |
| 前端框架 | Next.js 14 + React 18 | 14.2.18 / 18.3.1 |
| 前端样式 | Tailwind CSS | 3.4.16 |
| 密码哈希 | bcrypt | 4.2.1 |
| JWT | python-jose | 3.3.0 |
| 数据获取 | TanStack Query | 5.62.0 |

### 1.2 目录结构

```
auto-gallery/
├── backend/                  # Python FastAPI 后端
│   ├── app/
│   │   ├── api/              # 16 个路由模块
│   │   ├── jobs/             # 8 个 RQ 任务入口
│   │   ├── models/           # 19 个 SQLAlchemy 模型
│   │   ├── providers/        # 11 个平台 provider
│   │   ├── repositories/     # 5 个数据仓库
│   │   ├── schemas/          # 11 个 Pydantic schema
│   │   └── services/         # 28 个服务模块
│   ├── alembic/              # 数据库迁移
│   ├── tests/                # 26 个测试模块
│   └── scripts/              # 辅助脚本
├── admin-web/                # Next.js 管理前端
│   └── src/
│       ├── app/admin/        # 21 个页面路由
│       ├── app/api/v1/       # API 代理 (冗余 — 见 P0-2)
│       └── lib/              # 工具库、API 客户端
├── docs/                     # 文档
├── scripts/                  # 运维脚本
├── tests/e2e/                # E2E 浏览器测试
├── docker-compose.yaml       # 10 个服务
└── .env / .env.example        # 环境配置
```

### 1.3 Docker 服务拓扑

```
postgres (384M) ────┬── backend (1024M) ──── admin-web (256M)
redis (128M) ────────┤
meilisearch (—) ─────┤
                     ├── worker-download (768M) ×3 子进程
                     ├── worker-import (512M)
                     ├── worker-operations (512M)
                     └── scheduler (256M)
```

7 个应用容器，总内存预算约 3.5 GB。

### 1.4 核心数据流

```
subscription_source → download_job (enqueued → downloading → downloaded)
  → import_job (enqueued → running → complete)
  → Work / Asset / Tags → Meilisearch 索引 → 搜索可用
```

---

## 2. 启动 / 构建 / 测试命令

### 构建

```bash
# 构建所有镜像
docker compose build

# 仅构建后端
docker compose build backend

# 仅构建前端
docker compose build admin-web
```

### 启动

```bash
# 首次启动
cp .env.example .env   # 编辑 .env 替换所有 change-me-* 值
docker compose build
docker compose exec backend alembic upgrade head
docker compose up -d --force-recreate backend worker-download worker-import scheduler admin-web

# 后续日常启动
docker compose up -d
```

### 测试

```bash
# 后端测试（需在 Docker 内运行，依赖 PostgreSQL）
docker compose exec backend python -m pytest

# 仅运行非集成测试
docker compose exec backend python -m pytest -m "not integration"

# E2E 浏览器测试
cd tests/e2e && npx playwright test
```

### 开发

```bash
# 前端开发服务器
cd admin-web && npm run dev

# 后端 (需先启动 postgres/redis/meilisearch)
docker compose up -d postgres redis meilisearch
cd backend && uvicorn app.main:app --reload --port 8000
```

---

## 3. 风险清单

### P0 — 阻止启动 / 构建失败 / 核心流程崩溃

#### P0-1: `.env.ci` 硬编码凭据已提交到 Git

- **位置**: [`.env.ci`](.env.ci)
- **描述**: `.env.ci` 包含硬编码的 `POSTGRES_PASSWORD`、`REDIS_PASSWORD`、`MEILI_MASTER_KEY`、`SECRET_KEY`、`ADMIN_PASSWORD`，且被 Git 追踪（`.gitignore` 只忽略 `.env`，不忽略 `.env.ci`）。
- **影响**: CI 凭据泄露到仓库历史中。虽然 CI 环境通常是隔离的，但这些凭据可被任何有仓库读取权限的人获取。
- **修复**: 将 `.env.ci` 添加到 `.gitignore`；CI 通过 CI 系统的 secret 变量注入凭据。已提交的历史需要用 `git filter-branch` 清理。

#### P0-2: 前端 API 代理存在冗余死代码

- **位置**: [`admin-web/src/app/api/v1/[...path]/route.ts`](admin-web/src/app/api/v1/[...path]/route.ts)
- **描述**: Next.js 的 `next.config.js` 已配置 `rewrites()` 将 `/api/v1/:path*` 代理到 `http://backend:8000`。`route.ts` 中的手动代理代码是**死代码**——永远不会被执行，因为 rewrites 在服务端层面先拦截了请求。其中 `responseHeaders.delete("content-encoding")` 的逻辑有 bug：如果被复制到其他位置使用，会导致压缩响应乱码。
- **影响**: 维护者可能误以为此文件在生效，修改它而实际不产生效果。
- **修复**: 删除 `admin-web/src/app/api/v1/[...path]/route.ts`，在 `next.config.js` 添加注释说明 API 代理由 rewrites 处理。

### P1 — 核心功能可用但存在明显 Bug

#### P1-1: 登录接口无速率限制

- **位置**: [`backend/app/api/auth_api.py`](backend/app/api/auth_api.py)
- **描述**: `/api/v1/auth/login` 端点没有速率限制。攻击者可以暴力破解 admin 密码。虽然 admin 密码在首次登录后会强制修改（`must_change_password=True`），但如果设置了弱密码，仍可在短时间内被爆破。
- **影响**: 暴力破解风险。NAS 局域网部署降低了风险，但若通过反向代理暴露到公网则非常危险。
- **修复**: 添加基于 Redis 计数器的速率限制中间件，登录端点限制为 5 次/分钟/IP。

#### P1-2: JWT Token 存储在 localStorage，存在 XSS 风险

- **位置**: [`admin-web/src/lib/auth.tsx:86`](admin-web/src/lib/auth.tsx#L86)
- **描述**: JWT token 存储在 `localStorage` 中。任何 XSS 漏洞都可以读取 token 并盗用会话。管理面板虽然是内部使用的，但如果创作者名称、作品标题、标签中包含未转义的脚本内容，则存在注入可能。
- **影响**: XSS 可导致会话劫持。
- **修复**: Token 只存储在 httpOnly cookie 中，移除 `localStorage.setItem("ag_token", ...)`。

#### P1-3: 导入任务静默跳过解析失败的 Work

- **位置**: [`backend/app/jobs/import_runner.py:307-308`](backend/app/jobs/import_runner.py#L307-L308)
- **描述**: 当 provider 的 `parse_work_source()` 或 `parse_source_creator()` 抛出异常时，代码 `continue` 跳过该 work，只记录一条 warning 日志。如果某个 provider 的解析逻辑与新版本 gallery-dl 输出不兼容，**所有 works 都会被静默跳过**，下载任务仍显示 `complete`。
- **影响**: 下载成功但全部导入失败，管理员只能在发现作品数不增长时才能察觉。
- **修复**: 跟踪跳过计数，当跳过率超过 50% 时将 import_job 标记为 `failed`。

#### P1-4: `system.py` 日志级别排序逻辑脆弱

- **位置**: [`backend/app/api/system.py:554`](backend/app/api/system.py#L554)
- **描述**: `levels = sorted(set(...), key=lambda x: ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"].index(x) if x in [...] else 0)` — 如果出现不在列表中的日志级别，会被排到最前面（index 0 = DEBUG 的位置），打乱前端日志视图排序。
- **影响**: 日志视图在前端显示混乱。
- **修复**: 使用 `else len(LEVEL_ORDER)` 将未知级别排到最后。

#### P1-5: 下载任务 `except Exception: pass` 多处掩盖错误

- **位置**:
  - [`backend/app/jobs/download.py:339`](backend/app/jobs/download.py#L339) — `except Exception: logger.debug(...)`
  - [`backend/app/jobs/download.py:470-471`](backend/app/jobs/download.py#L470-L471) — Redis 健康检查失败 `pass`
- **描述**: 下载任务中有多处 `except Exception: pass` 或仅 debug log 的异常处理，可能掩盖真实的配置错误或运行时问题。
- **影响**: 问题可能在生产中持续存在而不被发现。
- **修复**: 至少记录 warning 级别日志并带 `exc_info=True`。

### P2 — 代码质量 / 重复逻辑 / 架构债 / 类型不严谨

#### P2-1: 巨型函数难以测试和维护

| 文件 | 函数 | 行数 |
|------|------|------|
| [`backend/app/jobs/download.py`](backend/app/jobs/download.py) | `run_download_job()` | ~975 |
| [`backend/app/jobs/import_runner.py`](backend/app/jobs/import_runner.py) | `run_import_job()` | ~555 |
| [`backend/app/jobs/subscription_sync.py`](backend/app/jobs/subscription_sync.py) | `_sync_subscriptions_locked()` | ~145 |

这些函数承担过多职责（下载、进度上报、错误恢复、manifest 记录、文件扫描），难以单独测试和修改。

#### P2-2: docker-compose 环境变量大量重复

- **位置**: [`docker-compose.yaml`](docker-compose.yaml)
- **描述**: 相同的环境变量块（DATABASE_URL、REDIS_URL、MEILI_*、SECRET_KEY 等）在 5 个服务中重复定义。修改一个变量需要改 5 处，容易遗漏。
- **修复**: 使用 YAML anchors (`&env`) / aliases (`*env`) 或 `env_file` 指令减少重复。

#### P2-3: Python 类型检查几乎不存在

- **位置**: [`backend/pyproject.toml:14`](backend/pyproject.toml#L14)
- **描述**: `ruff lint` 只检查 `["E9", "F63", "F7", "F82"]`（致命语法错误和未定义变量），没有启用任何类型检查规则。`requirements.txt` 中没有 mypy 或 pyright。
- **影响**: 运行时类型错误（如 None 属性访问）可能在生产中才暴露。

#### P2-4: TypeScript 类型文件过大

- **位置**: [`admin-web/src/lib/api/types.ts`](admin-web/src/lib/api/types.ts) — 818 行
- **描述**: 所有前端类型定义集中在一个文件中，应按领域（job、creator、curation 等）拆分。

#### P2-5: API 路由裸 `except Exception` 泛滥

- **位置**: `backend/app/api/` 下共 34 处 `except Exception:` 裸捕获
- **描述**: 部分异常处理只 log 不 re-raise，可能把数据库连接错误、内存错误当作业务异常吞掉。

#### P2-6: 调度器缺少 jitter（抖动）

- **位置**: [`backend/app/jobs/subscription_sync.py:399-403`](backend/app/jobs/subscription_sync.py#L399-L403)
- **描述**: 所有 scheduler 实例在同一时刻启动会在同一时刻触发扫描，多实例部署时可能产生惊群效应。
- **影响**: 单实例部署无影响，但代码缺少防御性 jitter。

#### P2-7: Dockerfile 中 gallery-dl 的 sed 补丁脆弱

- **位置**: [`backend/Dockerfile:27-28`](backend/Dockerfile#L27-L28)
- **描述**: `sed -i 's/^            state = reset = 2$/            return/'` 通过行内替换修补 gallery-dl 的 Twitter extractor。如果 gallery-dl 版本更新改变了缩进或逻辑，补丁会静默失效。
- **修复**: 添加补丁后验证（grep 检查修改是否生效），或使用 gallery-dl 的配置/postprocessor hook 机制替代。

#### P2-8: 测试覆盖率不透明

- **描述**: 项目有 26 个后端测试模块和 E2E 测试套件，但未配置覆盖率工具（如 `pytest-cov`）。无法量化测试覆盖率，无法识别未覆盖的关键路径。
- **修复**: 添加 `pytest-cov` 到开发依赖。

---

## 4. 推荐修复顺序

### 第一轮（立即修复，< 5 个文件）

| 优先级 | 编号 | 修复内容 | 影响文件数 |
|--------|------|----------|------------|
| 1 | P0-1 | `.env.ci` 加入 `.gitignore`，CI 凭据改用环境变量注入 | 2 |
| 2 | P0-2 | 删除死代码 `route.ts`，在 `next.config.js` 添加注释 | 2 |
| 3 | P1-1 | 添加登录速率限制（Redis 计数器中间件） | 2-3 |
| 4 | P1-3 | 导入跳过率超过阈值时标记 import_job 为 failed | 1 |

### 第二轮（短期修复）

| 优先级 | 编号 | 修复内容 |
|--------|------|----------|
| 5 | P1-2 | JWT token 改用 httpOnly cookie |
| 6 | P1-4 | 修复日志级别排序逻辑 |
| 7 | P2-7 | Dockerfile gallery-dl 补丁增加验证步骤 |

### 第三轮（可规划的重构）

| 优先级 | 编号 | 修复内容 |
|--------|------|----------|
| 8 | P2-1 | 拆分 `run_download_job()` 为独立阶段函数 |
| 9 | P2-2 | docker-compose.yaml 环境变量去重（YAML anchors） |
| 10 | P2-3 | 引入 mypy/pyright 类型检查 |

---

## 5. 不建议现在做的重构

以下事项在审计中被识别，但**建议暂缓**，避免引入新 Bug：

1. **将 gallery-dl 从子进程改为 Python API 调用** — gallery-dl 的 Python API 接口不稳定，子进程方式更可靠且隔离更好。
2. **将 RQ 迁移到 Celery** — RQ 对当前规模完全足够，Celery 的复杂性不值得。
3. **将 Meilisearch 替换为 Elasticsearch** — 功能满足需求，切换成本高且无实际收益。
4. **服务层大规模拆分** — 当前 28 个 service 模块已合理，进一步拆分会增加导入复杂度和循环依赖风险。
5. **引入微服务架构** — Docker Compose 单机部署是项目的设计目标（NAS LAN），微服务化违背设计意图。
6. **将 config.py 的启动时验证改为懒加载** — 当前 fail-fast 设计是有意为之的安全措施（CLAUDE.md 明确要求），不要弱化它。
7. **统一所有 API 路由的异常处理风格** — 34 处 `except Exception` 分散在 16 个路由文件中，逐一修改风险高且收益低，应在后续添加新功能时逐步改善。
8. **将前端从 Next.js pages router 迁移到 app router 的其他模式** — 当前架构稳定可用。

---

## 6. 正面发现

以下方面做得很好，值得保持：

- ✅ **Fail-fast 安全配置**: `config.py` 在启动时验证所有密钥，拒绝使用默认值启动
- ✅ **无 `shell=True`**: 所有 subprocess 调用使用列表形式传参，防止命令注入
- ✅ **路径遍历防护**: 使用 `Path.relative_to()` 而非字符串前缀匹配
- ✅ **共享连接池**: Redis（max 20）和 PostgreSQL（pool_size=2）连接池有上限，防止连接耗尽
- ✅ **Docker 资源限制**: 每个服务有 `mem_limit`，防止单容器 OOM 拖垮 NAS
- ✅ **Heartbeat + Stale 检测**: 两层机制（Redis TTL key + 数据库定时扫描）检测卡死的 worker
- ✅ **Partial import recovery**: 下载超时/中断后仍尝试导入已下载的文件
- ✅ **状态机验证**: `TaskEngine` 和 `task_state.py` 提供严格的状态转换验证
- ✅ **Manifest 事件记录**: 每个 job 有完整的审计轨迹（trigger、config、result、stats）
- ✅ **WebSocket + Redis pub/sub**: 实时进度推送，避免前端轮询
- ✅ **自动重试 + 退避**: 下载和导入失败后自动重试，使用指数退避
