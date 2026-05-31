# auto-gallery

[English version](README.md)

NAS 部署的多源媒体归档与画廊管理系统。

auto-gallery 从多个来源下载、导入、索引并管理以创作者为基础的媒体库。完全通过 Docker Compose 在 NAS 或任意 Linux 主机上运行。

## 系统架构

```
来源（Pixiv, X/Twitter, Iwara, Danbooru, Weibo, Bilibili, Pinterest, Lofter, 本地, 手动）
  → gallery-dl（仅 worker 执行）
    → DOWNLOAD_ROOT 按作品组织目录
      → 导入管线（元数据解析、哈希、标签、缩略图）
        → LIBRARY_ROOT 组织化元数据 + 缩略图
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

# 3. 启动全部服务
docker compose up -d

# 4. 运行数据库迁移（仅首次）
docker compose exec backend alembic upgrade head

# 5. 验证
curl http://localhost:8818/api/v1/system/health
```

管理后台：`http://<host>:13000`。

详细安装说明见 [docs/setup.zh.md](docs/setup.zh.md)。

## 服务

| 服务 | 角色 |
|---|---|
| `backend` | FastAPI REST API（端口 8818） |
| `worker` | 后台作业执行器（gallery-dl 下载、导入管线） |
| `scheduler` | 订阅同步调度器（定时或定点，支持时区配置） |
| `postgres` | 主数据库 |
| `redis` | RQ 任务队列 + 缓存 |
| `meilisearch` | 全文搜索引擎 |
| `admin-web` | Next.js 管理界面（端口 13000） |

`backend`、`worker`、`scheduler` 共用同一 Docker 镜像，通过不同命令启动。

## 支持的来源

| 来源 | 下载方式 | 状态 |
|---|---|---|
| Pixiv | gallery-dl | 完整支持（Cookie + Refresh Token 认证） |
| X/Twitter | gallery-dl | 可下载（时间线、媒体、喜欢；Cookie 认证） |
| Iwara | gallery-dl | 可下载（视频、图片；用户名/密码或 Cookie 认证） |
| Danbooru | gallery-dl | 可下载（标签搜索帖子；API Key 或用户名/密码认证） |
| Weibo | gallery-dl | 可下载（用户时间线、微博；可选 Cookie 认证） |
| Bilibili | gallery-dl | 可下载（用户文章、收藏；无需认证） |
| Pinterest | gallery-dl | 可下载（画板、Pin；公开 API，无需认证） |
| Lofter | gallery-dl | 可下载（博客文章；无需认证） |
| 本地文件夹 | 直接导入 | 计划中 |
| 手动上传 | 管理后台 | 计划中 |

所有已注册来源均支持 gallery-dl 下载。各来源的 `auto_enable_on_import` 可在 gallery-dl 设置中配置。

## 管理后台功能

- **仪表盘** — 系统健康、服务状态、存储概览
- **创作者** — 增删改查、来源账号绑定、Danbooru 身份映射、合并重复、发布活动网格图（含作品链接）
- **订阅** — 按创作者管理，支持按来源启用/禁用
- **任务** — 下载与导入队列管理、批量暂停/恢复/重试/删除、内联导入任务视图
- **作品** — 浏览器支持来源筛选、排序、批量删除/标签、作品详情含完整元数据
- **标签** — 标签管理、别名、合并、分类显示
- **调度器** — 统一同步 + 下载配置（调度模式、间隔、时区、超时、重试、跳过 AI）、队列统计、订阅同步表
- **数据中心** — 按来源和创作者的存储分布（前 20）、完整性检查（孤立文件、缺失缩略图、死链）、清理工具、备份与恢复、危险区域
- **设置** — 各来源 gallery-dl 提取器配置含连接测试、去重设置、代理配置含连通性测试、认证状态、命名模板、日志、语言切换、搜索重建索引

## 关键设计决策

- **通用领域模型** — `creator`、`work`、`asset`、`tag`（非 Pixiv 专用）
- **跨来源创作者身份** — 一个创作者，多个平台账号，含置信度评分
- **多来源订阅** — 订阅创作者后可自选同步哪些来源
- **去重默认关闭** — 计算哈希，生成候选，管理员决定
- **gallery-dl 仅 worker 执行** — API 绝不直接调用；安全 `shell=False` 子进程执行
- **代码中只用容器路径** — NAS 主机路径仅在 docker-compose.yaml 中
- **JWT 认证** — 管理员登录，access/refresh 令牌，自动刷新
- **时区感知调度** — 按部署配置，支持定时或间隔同步

## 开发

详见 [docs/development.zh.md](docs/development.zh.md)。

## 约束

本项目遵循严格的架构约束。详见 `.claude/constraints/`。最关键的约束：

1. 核心代码中禁止出现 Pixiv 专用模型名
2. Danbooru 既是下载来源，也是创作者身份参考来源
3. 去重为主动选择，绝不自动删除
4. 所有 subprocess 调用必须 `shell=False`
5. 应用代码中禁止硬编码 NAS 主机路径
6. Admin API 需 JWT 认证
7. v1 不暴露到公网

## 许可证

待定
