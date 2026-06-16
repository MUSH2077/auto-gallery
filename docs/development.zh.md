# 开发指南

## 项目结构

```text
auto-gallery/
  docker-compose.yaml
  .env.example
  README.md
  README.zh.md
  docs/
    architecture.md    architecture.zh.md
    setup.md           setup.zh.md
    development.md     development.zh.md
    providers.md       providers.zh.md
    risks.md           risks.zh.md
  backend/
    Dockerfile
    requirements.txt
    alembic.ini
    app/
      main.py
      config.py
      database.py
      models/
      schemas/
      repositories/
      services/
      api/
      providers/
      jobs/
    alembic/
      env.py
      versions/
    tests/
  admin-web/
    Dockerfile
    package.json
    tsconfig.json
    tailwind.config.ts
    src/
      app/
        admin/           # 全部管理页面
          creators/      # 列表、详情、身份映射、去重
          subscriptions/ # 列表、详情
          jobs/          # 下载+导入统一队列
          works/         # 列表、详情
          tags/          # 标签管理
          scheduler/     # 同步计划与队列
          search/        # Meilisearch 全文搜索
          reference/     # Danbooru 参考映射
          sources/       # 数据源能力矩阵
          system/        # 系统健康仪表盘
          settings/      # gallery-dl、去重、代理、认证状态、日志、备份
          data-mgmt/     # 存储统计、完整性检查、危险区域
          merge-candidates/
          dedup/
          login/
      components/        # 共享组件
        ConfirmDialog.tsx
        DataTable.tsx
        EmptyState.tsx
        ErrorBoundary.tsx
        ErrorState.tsx
        LoadingSkeleton.tsx
        Modal.tsx
        PageHeader.tsx
        SourceBadge.tsx
        StatusBadge.tsx
        Toast.tsx
        WorkGrid.tsx
      lib/               # 类型化 API 客户端、i18n、认证、主题
        api.ts
        auth.tsx
        i18n.tsx
        theme.tsx
```

## 后端开发

### 技术栈

- Python 3.12
- FastAPI
- SQLAlchemy 2.0（异步）
- Alembic
- PostgreSQL 16
- Redis（RQ）
- Meilisearch

### 端口

| 上下文          | 后端   | 管理前端 |
|-----------------|--------|----------|
| 主机（映射）    | 8818   | 13000    |
| 容器内部        | 8000   | 3000     |

### 本地运行（不用 Docker 快速迭代）

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 需要先通过 Docker 启动 postgres、redis、meilisearch
docker compose up -d postgres redis meilisearch

# 启动 API（容器内部端口 8000）
uvicorn app.main:app --reload --port 8000
```

### 运行测试

```bash
# 全部测试
docker compose run --rm -e PYTHONDONTWRITEBYTECODE=1 backend python -m pytest

# 单个测试文件
docker compose run --rm -e PYTHONDONTWRITEBYTECODE=1 backend python -m pytest tests/test_providers.py

# 含覆盖率
docker compose run --rm -e PYTHONDONTWRITEBYTECODE=1 backend python -m pytest --cov=app
```

如果使用本地虚拟环境，安装和 CI 相同的依赖集：

```bash
cd backend
source venv/bin/activate
pip install -r requirements.txt
python -m pytest
ruff check app tests
```

测试套件会在 `tests/conftest.py` 中先设置安全的测试默认值，因此本地运行不需要生产密钥。

如果之前用 root 或容器在宿主机源码树里留下了不可写的字节码缓存，可以先清理：

```bash
sudo find backend -type d \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +
```

### Python 依赖锁

后端运行时和 CI 都安装 `requirements.txt`；它由 `requirements.in` 通过
`pip-tools` 生成。

```bash
cd backend
python -m pip install pip-tools
pip-compile --resolver=backtracking --allow-unsafe --output-file requirements.txt requirements.in
```

像 `gallery-dl` 这类 provider 敏感依赖必须固定版本；升级后先跑 Docker
Compose smoke test 再合并。

### 数据库迁移

```bash
# 创建新迁移
docker compose run --rm backend alembic revision --autogenerate -m "描述"

# 应用迁移
docker compose run --rm backend alembic upgrade head

# 回滚一步
docker compose run --rm backend alembic downgrade -1
```

### 迁移安全

- 提交前务必审查 `--autogenerate` 生成的迁移内容
- 注意：删表/删列、约束变更、列重命名（autogenerate 会误判为删除+新增）
- 如果迁移会丢失数据，必须在 commit message 中明确说明理由
- 合并前测试 `alembic upgrade head` 和 `alembic downgrade -1`

### 代码规范

- 业务逻辑在 services 中，绝不在路由处理函数中
- 数据库访问通过 repositories，绝不在 services 中直接操作
- 全部 API 输入输出使用 Pydantic schema
- Provider 模块不可访问数据库
- 原始来源元数据存储在 JSONB 字段
- 导入逻辑幂等（重复运行不产生重复数据）

## Provider 开发

详见 [docs/providers.zh.md](providers.zh.md)。

## Admin Web 开发

### 技术栈

- Next.js 14 (14.2.18)
- React 18 (18.3.1)
- TypeScript (5.7.2)
- Tailwind CSS (3.4.16)
- TanStack Query (5.62.0)

### 页面

管理前端已完整构建，包含以下页面：

| 模块 | 功能 |
| --- | --- |
| 仪表盘 | 系统健康、存储、最近活动 |
| 创作者 | 列表、详情、身份映射、去重 |
| 订阅 | 列表、详情（多源配置） |
| 任务 | 下载 + 导入统一队列 |
| 作品 | 网格/列表视图、详情（含来源记录和资源文件） |
| 标签 | 标签管理（搜索、分类） |
| 调度器 | 同步计划、队列状态、调度器配置 |
| 搜索 | Meilisearch 全文搜索（作品、创作者、标签） |
| Danbooru 参考 | 画师搜索、URL 批量导入、Pixiv ID 搜索 |
| 数据源 | 数据源能力矩阵、URL 验证 |
| 设置 > gallery-dl | 按来源提取器配置（认证、内容过滤、文件组织、速率限制） |
| 设置 > 去重 | 源级、跨源、感知哈希去重开关 |
| 设置 > 代理 | HTTP/HTTPS 代理配置（gallery-dl 和 API 调用） |
| 设置 > 认证状态 | 各订阅来源 Cookie/Token 健康监控 |
| 设置 > 日志 | 内存环形缓冲区实时日志查看器 |
| 设置 > 备份与恢复 | 系统完整备份创建和恢复 |
| 设置 > 下载默认值 | 超时、重试、退避、最大帖子数 |
| 设置 > 订阅默认值 | 同步间隔、调度模式、时区 |
| 数据管理 | 存储统计、完整性检查、清理工具、危险区域 |

### 本地运行

```bash
cd admin-web
npm install

# 启动开发服务器（端口 3000）
npm run dev
```

开发服务器通过 `BACKEND_INTERNAL_URL` 环境变量将 API 请求代理到后端。

### 生产构建

```bash
npm run typecheck
npm run build
```

### 前端调试

```bash
# 检查 TypeScript/构建错误
npm run typecheck
npm run build

# 构建输出会显示所有类型错误和编译问题
```

### i18n（国际化）

管理前端支持中文和英文。所有 UI 字符串在 `src/lib/i18n.tsx` 中定义为 `zh` 和 `en` 两组值。语言偏好存储在 localStorage，默认跟随浏览器语言。

新增 UI 时：

1. 为页面中出现的所有文本添加 i18n key
2. 中文值应对中文用户阅读自然流畅
3. 英文值应对英文用户阅读自然流畅
4. 绝不直接展示原始的 i18n key ID——如果你在页面上看到 key 而不是翻译文本，说明 i18n.tsx 中缺少对应的 key

Key 格式：`namespace.natural_name`（例如 `jobs.download`、`creators.title`）。

### 前端规范

- 类型化 API 客户端在 `src/lib/api.ts`
- 可复用组件集中放在 `src/components/`
- 页面保持轻量；TanStack Query 处理服务端状态
- 移动端友好但桌面端优先
- 深色模式支持，通过 `src/lib/theme.tsx` 实现

## 调试

### 查看日志

```bash
# 全部服务
docker compose logs -f

# 特定服务
docker compose logs -f worker-download
docker compose logs -f worker-import

# 最近 100 行
docker compose logs --tail=100 backend
```

### 访问数据库

```bash
docker compose exec postgres psql -U autogallery
```

### 检查 Redis 队列

```bash
docker compose exec redis redis-cli -a $REDIS_PASSWORD
> KEYS *
> LLEN rq:queue:default
> LLEN rq:queue:downloads
> LLEN rq:queue:imports
> LLEN rq:queue:scheduled
```

### 手动测试 gallery-dl

```bash
docker compose exec worker-download gallery-dl --version
docker compose exec worker-download gallery-dl --config /gallerydl-config/config.json "https://www.pixiv.net/artworks/123456"
```

### 依赖变更后重建

```bash
docker compose build backend
docker compose up -d backend worker-download worker-import scheduler

# 或管理前端
docker compose build admin-web
docker compose up -d admin-web
```

### 运行时验收

```bash
docker compose build backend admin-web
docker compose up -d backend admin-web worker-download worker-import scheduler
scripts/verify-runtime.sh
```

使用仓库内安全 CI 环境做运行时验证：

```bash
docker compose --env-file .env.ci config --quiet
docker compose --env-file .env.ci build backend admin-web
docker compose --env-file .env.ci up -d
COMPOSE_ENV_FILE=.env.ci scripts/verify-runtime.sh
docker compose --env-file .env.ci down -v
```

`.env.ci` 使用 Docker named volumes，而不是仓库内 `data/ci` bind mount；
因此 `down -v` 会清掉容器写入的运行数据，不会在工作区留下 root-owned 文件。
