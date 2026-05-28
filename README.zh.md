# auto-gallery

[English version](README.md)

NAS 部署的多源媒体归档与画廊管理系统。

auto-gallery 从多个来源下载、导入、索引并管理以创作者为基础的媒体库。完全通过 Docker Compose 在 NAS 或任意 Linux 主机上运行。

## 系统架构

```
来源（Pixiv, Iwara, X, 本地, 手动, Danbooru 参考）
  → gallery-dl（仅 worker 执行）
    → DOWNLOAD_ROOT 暂存区
      → 导入管线（元数据解析、哈希、标签）
        → LIBRARY_ROOT 组织化媒体库
          → PostgreSQL（规范数据模型）
          → Meilisearch（全文搜索）
          → Admin Web（Next.js）
```

核心原则：所有领域模型与来源无关。Provider 模块封装来源特有行为。一个规范 `creator` 可映射到跨平台的多个 `source_creator` 账号。

完整架构详见 [docs/architecture.zh.md](docs/architecture.zh.md)。

## 快速开始

```bash
# 1. 克隆
git clone <repo-url> auto-gallery && cd auto-gallery

# 2. 配置
cp .env.example .env
# 编辑 .env —— 设置密码和主机路径

# 3. 启动基础设施
docker compose up -d postgres redis meilisearch

# 4. 运行数据库迁移
docker compose run --rm backend alembic upgrade head

# 5. 启动全部服务
docker compose up -d

# 6. 验证
curl http://localhost:8818/api/v1/system/health
```

管理后台：`http://<host>:3000`。

详细安装说明见 [docs/setup.zh.md](docs/setup.zh.md)。

## 服务

| 服务 | 角色 |
|---|---|
| `backend` | FastAPI REST API |
| `worker` | 后台作业执行器（gallery-dl、导入） |
| `scheduler` | 订阅调度器 |
| `postgres` | 主数据库 |
| `redis` | 任务队列 + 缓存 |
| `meilisearch` | 全文搜索引擎 |
| `admin-web` | Next.js 管理界面 |

`backend`、`worker`、`scheduler` 共用同一 Docker 镜像，通过不同命令启动。

## 支持的来源

| 来源 | 下载方式 | 状态 |
|---|---|---|
| Pixiv | gallery-dl | 首个支持 |
| Iwara | 待定 | 占位 |
| X/Twitter | 待定 | 占位（暂无时间表） |
| Danbooru | 仅供参考 | 创作者身份映射 |
| 本地文件夹 | 直接导入 | 计划中 |
| 手动上传 | 管理后台 | 计划中 |

## 关键设计决策

- **通用领域模型** — `creator`、`work`、`asset`、`tag`（非 Pixiv 专用）
- **跨来源创作者身份** — 一个创作者，多个平台账号
- **多来源订阅** — 订阅创作者后自选同步哪些来源
- **去重默认关闭** — 计算哈希，生成候选，管理员决定
- **gallery-dl 仅 worker 执行** — API 绝不能直接调用；安全子进程执行
- **代码中只用容器路径** — NAS 主机路径仅在 docker-compose.yaml 中
- **管理员密钥仅服务端持有** — Admin Web 通过 Next.js 服务端路由转发 `/api/v1/*`，并用 `ADMIN_PASSWORD` 注入 `X-Admin-Key`，浏览器不再暴露管理员密钥

## 开发

详见 [docs/development.zh.md](docs/development.zh.md)。

## 约束

本项目遵循严格的架构约束。详见 `.claude/constraints/`。最关键的约束：

1. 核心代码中禁止出现 Pixiv 专用模型名
2. Danbooru 是参考来源，非下载来源（当前阶段）
3. 去重为主动选择，绝不自动删除
4. 所有 subprocess 调用必须 `shell=False`
5. 应用代码中禁止硬编码 NAS 主机路径
6. Admin API 需要认证
7. v1 不暴露到公网

## 许可证

待定
