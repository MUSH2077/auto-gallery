# 开发指南

## 项目结构

```
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
  admin-web/          # 后续
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

### 本地运行（不用 Docker 快速迭代）

```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 需要先通过 Docker 启动 postgres、redis、meilisearch
docker compose up -d postgres redis meilisearch

# 启动 API
uvicorn app.main:app --reload --port 8818
```

### 运行测试

```bash
# 全部测试
docker compose run --rm backend pytest

# 单个测试文件
docker compose run --rm backend pytest tests/test_providers.py

# 含覆盖率
docker compose run --rm backend pytest --cov=app
```

### 数据库迁移

```bash
# 创建新迁移
docker compose run --rm backend alembic revision --autogenerate -m "描述"

# 应用迁移
docker compose run --rm backend alembic upgrade head

# 回滚一步
docker compose run --rm backend alembic downgrade -1
```

### 代码规范

- 业务逻辑在 services 中，绝不在路由处理函数中
- 数据库访问通过 repositories，绝不在 services 中直接操作
- 全部 API 输入输出使用 Pydantic schema
- Provider 模块不可访问数据库
- 原始来源元数据存储在 JSONB 字段
- 导入逻辑幂等（重复运行不产生重复数据）

### 迁移安全

- 提交前务必审查 `--autogenerate` 生成的迁移内容
- 注意：删表/删列、约束变更、列重命名（autogenerate 会误判为删除+新增）
- 如果迁移会丢失数据，必须在 commit message 中明确说明理由
- 合并前测试 `alembic upgrade head` 和 `alembic downgrade -1`

## Provider 开发

详见 [docs/providers.zh.md](providers.zh.md)。

## Admin Web 开发（后续）

### 技术栈
- Next.js 14+
- React 18+
- TypeScript
- Tailwind CSS
- TanStack Query

### 规范
- 类型化 API 客户端
- 可复用组件集中放在 `src/components/`
- 页面保持轻量；TanStack Query 处理服务端状态
- 移动端友好但桌面端优先

## 调试

### 查看日志

```bash
# 全部服务
docker compose logs -f

# 特定服务
docker compose logs -f worker

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
```

### 手动测试 gallery-dl

```bash
docker compose exec worker gallery-dl --version
docker compose exec worker gallery-dl --config /gallerydl-config/config.json "https://www.pixiv.net/artworks/123456"
```

### 依赖变更后重建

```bash
docker compose build backend
docker compose up -d backend worker scheduler
```
