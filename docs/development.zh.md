# 开发指南

请先阅读[中文贡献指南](../CONTRIBUTING.zh.md)，了解贡献流程、必要检查、隐私规则和
PR 要求。

## 仓库结构

```text
auto-gallery/
  backend/
    app/
      api/            FastAPI 路由
      models/         SQLAlchemy 模型
      repositories/   数据库访问
      schemas/        API 契约
      services/       业务逻辑与编排
      providers/      来源 URL 与元数据适配
      jobs/           后台任务入口
    alembic/          数据库迁移
    tests/
  admin-web/
    src/
      app/admin/      App Router 页面和布局
      components/     共享 UI 原语与领域组件
      lib/api/        类型化 API 客户端
      lib/            认证、国际化、导航、主题与偏好
    tests/e2e/        Playwright 浏览器测试
  docs/
  scripts/
  docker-compose.yaml
```

当前 Docker Compose 服务：

- `postgres`、`redis`、`meilisearch`
- `backend`
- `worker-download`、`worker-import`、`worker-operations`、`worker-discovery`
- `scheduler`
- `admin-web`

API 和 Python worker 共用 backend 镜像，只有下载 worker 执行 gallery-dl。

## 后端

### 技术栈

Python 3.12、FastAPI、SQLAlchemy 2.0 async、Alembic、PostgreSQL 16、
Redis/RQ、Meilisearch 和 gallery-dl。

### 本地迭代

先启动数据服务，再从虚拟环境运行 API：

```bash
docker compose up -d postgres redis meilisearch

cd backend
python -m venv venv
source venv/bin/activate
python -m pip install --require-hashes -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Compose 映射的后端地址为 `http://localhost:8818`；本地 Uvicorn 默认使用
`http://localhost:8000`。

### 测试与检查

容器路径最容易复现：

```bash
docker compose run --rm -T --volume "$PWD/backend:/app" \
  -e PYTHONDONTWRITEBYTECODE=1 backend \
  python -m pytest
docker compose run --rm -T --volume "$PWD/backend:/app" \
  -e PYTHONDONTWRITEBYTECODE=1 backend \
  ruff check app tests
```

单文件测试可在 `pytest` 后追加路径。本地虚拟环境使用相同的 Python 命令，但不需要
前面的 `docker compose run ... backend`。

### 依赖

`backend/requirements.txt` 由 `requirements.in` 通过 pip-tools 生成。
gallery-dl 等 Provider 敏感依赖必须固定版本。

```bash
cd backend
python -m pip install pip-tools
pip-compile --allow-unsafe --generate-hashes \
  --no-emit-index-url --no-strip-extras \
  --output-file requirements.txt requirements.in
```

升级依赖后运行 Provider 测试和 Docker Compose smoke 路径。
Dependabot 会将 minor 与 patch 更新分组到同一个 PR，但每个 major 更新保持独立 PR，
以便单独审查迁移工作与兼容性风险。

### 数据库迁移

```bash
docker compose run --rm backend \
  alembic revision --autogenerate -m "description"
docker compose run --rm backend alembic upgrade head
docker compose run --rm backend alembic downgrade -1
```

必须审查自动生成内容。列重命名经常被识别为破坏性的删除和新增。PR 中需要说明回填、
重建索引、长时间锁表和数据丢失风险，并验证 upgrade 与 downgrade。

### 模块边界

- 路由负责认证、校验和委托。
- Service 负责业务规则与编排。
- Repository 负责数据库访问。
- Provider 负责 URL 校验、规范化和来源元数据解析，不直接访问数据库。
- 导入与维护操作必须幂等。
- 后台状态转换必须遵守既有任务状态机。

## 管理前端

### 技术栈与结构

前端使用 Next.js 16.3.6、React 19.2.8、TypeScript、Tailwind CSS、TanStack Query 和
Playwright。主要产品模块位于 `src/app/admin`；共享页面容器、导航、反馈、对象列表、
文本溢出和交互原语位于 `src/components`。

API 访问拆分在 `src/lib/api/client.ts`、`types.ts`、`types.generated.ts` 和
`index.ts`。先将后端 OpenAPI 契约导出至 `docs/api/openapi.json`，再从该文件生成前端类型：

```bash
cd admin-web
npm run generate:api-types
```

### 本地运行与检查

```bash
cd admin-web
npm ci
npm run dev
```

提交 PR 前：

```bash
npm run typecheck
npm run build
npm run test:e2e
```

环境中没有 Playwright 浏览器时：

```bash
npx playwright install chromium
```

### 前端要求

- 所有面向用户的文字同时提供中英文。
- 页面层级复用共享导航元数据，不在各页面重复定义。
- 检查窄屏和长文本，不依赖固定像素宽度。
- 保留键盘焦点、触屏目标、语义名称和降低动效支持。
- 把加载、空、错误、部分成功和无权限状态作为一等界面。
- 当主要工作流与公开图片明显不同时，更新脱敏文档素材。

## 仓库检查

CI 的 Node 回归组运行 `tests/*.test.mjs`、`tests/*-contract.mjs`、
`tests/*.contract.mjs` 及明确列出的 Jobs 和任务动作测试。
`helpers.mjs`、`typescript-loader.mjs` 是辅助模块；`danbooru-import-flow`、
`test-client-nav`、`test-processing-nav`、`test-result-restore` 是手动浏览器工具，
不作为 Node test runner 输入。新增行为回归放入这些组或既有后端／Playwright 分片，
不另建冻结覆盖清单。

Cookie 路径应通过配置读取、保存和有效下载配置构造验证，越界和符号链接使用无敏感内容
的临时文件。上传同时验证真实请求流、multipart 解析和并发 PostgreSQL 保存：
最多 20 个文件、单文件 500 MiB、批次 1 GiB，并预留 2 MiB multipart 开销。
前置预算不能替代最终原子计费；注入读取失败、取消、移动和提交失败后，核对临时文件及用量补偿。

缩略图 GET 和条件 GET 必须在读取及派生请求前验证会话／Bearer 或绑定资产和用途的票据。
同源浏览器图片会携带会话 Cookie，需保留 private 缓存和 ETag 行为，另测匿名和已撤销会话。

回滚测试使用命令替身，不接触在线部署。停止失败、超时或 writer 仍活跃时，
切换和前台启动必须阻断。查看 `journal.json` 或 `rollback-receipt.env`，
解决停止问题后重跑原回滚命令。主机测试的命令替身应使用与测试一致的 Python 版本，
将测试虚拟环境的 `bin` 放在 PATH 前面。

```bash
docker compose --env-file .env.ci config --quiet
bash scripts/privacy-scan.sh
bash scripts/package-release.sh ci
```

CI 还会运行完整后端、前端、仓库和 Compose smoke 检查。详见
[.github/workflows/ci.yml](../.github/workflows/ci.yml)。
