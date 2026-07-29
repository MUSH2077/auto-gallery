# 系统架构

## 概述

auto-gallery 是一个分层的 Docker Compose 应用，从多个来源下载媒体，将元数据导入规范的 PostgreSQL 数据库，并通过 FastAPI 后端和 Next.js 管理界面提供服务。

## 分层图

```
┌─────────────────────────────────────────────────┐
│  管理端 (Next.js 14)                             │
│  TypeScript · Tailwind CSS · TanStack Query      │
│  端口 13000（主机）← 3000（容器）                 │
└──────────────────┬──────────────────────────────┘
                   │ HTTP REST
┌──────────────────▼──────────────────────────────┐
│  后端 (FastAPI)                                  │
│  Routes → Services → Repositories → SQLAlchemy   │
│  Providers: Pixiv · X · Iwara · Danbooru ·      │
│   Weibo · Bilibili · Pinterest · LOFTER          │
│   (+ Danbooru reference；local/manual 计划中)    │
│  端口 8818（主机）← 8000（容器）                  │
│  JWT 认证（access + refresh token）              │
└──────┬────────────┬──────────────┬──────────────┘
       │            │              │
       │   ┌────────▼────────────┐ │
       │   │  Workers (RQ)       │ │
       │   │  worker-download    │ │  gallery-dl 子进程
       │   │  worker-import      │ │  元数据 → DB 管线
       │   │  worker-operations  │ │  备份 / 重建索引 / 磁盘导入
       │   │  scheduler          │ │  订阅同步循环
       │   └────────┬────────────┘ │
       │            │              │
┌──────▼────────────▼──────────────▼──────────────┐
│  数据层                                          │
│  PostgreSQL ── Redis ── Meilisearch              │
│  ┌──────────┐ ┌──────┐ ┌─────────────┐          │
│  │ /downloads│ │/lib │ │ /gallerydl  │          │
│  │           │ │rary │ │ -config     │          │
│  └──────────┘ └──────┘ └─────────────┘          │
└─────────────────────────────────────────────────┘
```

## 领域模型

所有模型使用与来源无关的命名。共享数据 vs 用户隔离数据：

```
（跨用户共享）
creator ──< source_creator
creator ──< creator_link
work ──< work_source
work ──< work_tag
work_source ──< work_source_tag
asset ──< asset_source
tag

（用户隔离）
user ──< subscription ──< subscription_source
user ──< album ──< album_work ── work
user ──< download_job
import_job（通过 subscription 继承隔离）

（运维 / 横切）
task_run ──< task_event        # 统一任务信封 —— 见"统一任务系统"
storage_artifact               # 下载 → 导入账本 —— 见"任务队列"
```

### 关键关系

- **user**：用户账号（管理员或普通用户）。Phase 6+ 添加。
- **creator**：规范本地身份（如"画师 A"）——跨所有用户共享
- **source_creator**：平台特定账号（如 Pixiv 用户 123456）
- **creator_link**：链接创作者到外部个人资料的 URL
- **work**：规范本地作品（可有多个 work_source）
- **work_source**：来源特定的作品记录，保留原始元数据
- **asset**：本地文件
- **asset_source**：来源特定的文件记录
- **subscription**：用户追踪创作者的意图
- **subscription_source**：每个订阅的逐来源开关
- **album**：用户创建的作品收藏集（Phase 6+）
- **album_work**：album 与 work 的多对多关联

### 唯一性约束

- `(source, source_creator_id)` 在 source_creators 表
- `(source, source_work_id)` 在 work_sources 表
- `(source, source_asset_id)` 在 asset_sources 表
- `(normalized_name)` 在 tags 表
- `(user_id, creator_id)` 在 subscriptions 表（多用户可订阅同一创作者）
- `(subscription_id, source)` 在 subscription_sources 表
- `(username)` 在 users 表
- `(album_id, work_id)` 在 album_works 表

## Provider 系统

来源特定行为封装在 `backend/app/providers/` 下的 provider 模块中。

每个 provider 实现：
- URL 规范化和验证
- gallery-dl 配置生成（如果可下载）
- 元数据解析（source_creator、work_source、asset、tag）

Provider 绝不访问数据库。它们接收原始元数据字典并返回数据字典。服务层负责持久化。

## 任务队列 (RQ)

auto-gallery 使用 RQ（Redis Queue）做下载与批量导入，并配合一个 `operations` 队列做管理操作（备份、重建索引、磁盘导入）。关键设计决策：

- **为什么用 RQ**：比 Celery 更简单，使用已有的 Redis。`download_job`/`import_job` 数据库表是真实数据源，队列后端可替换。
- **逐来源下载队列**：每个来源有独立的 RQ 队列（`downloads:pixiv`、`downloads:danbooru` 等）以隔离——一个慢来源不会阻塞另一个。`worker-download` 容器监听所有来源队列。
- **持久化 RQ 导入**：下载产物记录在 PostgreSQL 中，单一 RQ 导入管线以租约（lease）认领工作，实现无竞争消费者的可重启恢复。
- **任务超时**：所有入队调用使用 `job_timeout=7200`（2 小时），防止 RQ 杀掉长时间运行的下载任务（默认 180s）。
- **代理预检**：启动 gallery-dl 前，worker 先做 DNS 解析 + 代理连通性检查（非阻塞诊断）。
- **状态机**（权威来源：`backend/app/models/task_state.py`）：`enqueued → downloading → downloaded → importing → complete`，非终态可转入 `paused`、`cancelled`、`failed`、`stale`。旧状态串（`pending` → `enqueued`）通过向后兼容映射接受。存在 `downloaded → complete` 短路：当下载未产生新元数据（内容已在归档中）时直接完成，避免卡在等待导入。暂停任务跳过执行；`cancelled` 为终态但保留数据库记录以便审计。僵死检测使用 Redis 心跳键（`task:{job_id}:heartbeat_ts`，90s TTL）。超时/失败时支持部分导入恢复。`TaskEngine`（`services/task_engine.py`）是校验所有状态转换的唯一权威。

## 统一任务系统 (`task_runs`)

下载、导入、管理操作（备份、重建索引、磁盘导入）都通过一个面向用户的任务信封呈现，使管理端**任务**页（`/admin/jobs`）拥有统一的队列/进度/历史视图。

- **`task_run`**（`models/task_run.py`）：每个可追踪工作单元一行。`kind`（`download` / `import` / `admin`）加 `subject_type` + `subject_id` 以松耦合方式引用权威领域行（`download_job`、`import_job` 等）——无外键，因此任务可以先于或晚于其领域记录存在。携带展示字段（`title`、`source`、`source_url`）、进度快照（`progress_current`/`progress_total`/`progress_data`）、`priority`、`attempts`，以及用于僵死检测的 `last_heartbeat_at`。`parent_task_id` 用于归组批量/子任务。
- **`task_event`**：某次运行状态转换的追加式审计轨迹；随父记录级联删除（`ON DELETE CASCADE`）。
- **`TaskService`**（`services/tasks.py`）：写入路径——创建运行、记录事件、规范化状态，并把下载/导入领域行合并进统一列表。
- **`TaskEngine`**（`services/task_engine.py`）：状态转换的唯一权威（暂停 / 恢复 / 取消 / 重试 / 优先级 / 按筛选批处理），并通过 Redis pub/sub 发布变更以驱动 WebSocket 实时更新。
- **权威载荷仍在领域表**：`task_run` 是信封；`download_jobs` / `import_jobs` 仍是各自字段的真实数据源。清空任务（数据管理）会同时删除 `task_runs` **和**领域任务表。

## 数据流

### 下载流程
```
调度器（时区感知，可配置间隔或每日定时）
  → 检查每个 subscription_source：是否到同步时间？
    → 创建 download_job（pending）
      → Worker 取走任务（downloading）
        → Provider.build_gallerydl_config()
        → subprocess.run(["gallery-dl", "--write-metadata",
            "--range", "1-{max_posts}", ...], shell=False)
        → 文件落盘到 DOWNLOAD_ROOT/{source}/{creator_name}/{source_work_id}/
        → 任务状态 → downloaded
        → 入队 import_job
```

下载关键细节：
- `--write-metadata`：必需标志——没有它，导入运行器就没有 JSON 元数据可解析。
- `--range`：限制下载最近 N 篇文章用于增量同步。每个订阅可配置。
- **调度器**：通过 `TIMEZONE` 环境变量支持时区（默认 UTC）。支持间隔模式（每 N 小时同步）或定时模式（每日指定时间同步）。

### 导入流程
```
Worker 取走 import_job
  → 扫描 DOWNLOAD_ROOT/{source}/ 下的 *.json 元数据文件（--write-metadata 输出）
  → Provider.parse_work_source() → 按 source_work_id 归组 JSON
  → Provider.parse_source_creator() → upsert source_creator
  → Provider.parse_work_source() → upsert work_source
  → 从作品目录中的图像文件创建 Asset + AssetSource
  → Provider.parse_source_tags() → upsert tags + work_source_tags
  → 生成缩略图（pyvips WebP）到 LIBRARY_ROOT/{source}/{creator_name}/{source_work_id}/
  → 写出 metadata.json 到 LIBRARY_ROOT/{source}/{creator_name}/{source_work_id}/
  → 删除已处理的 JSON 文件（图片文件保留在 DOWNLOAD_ROOT）
  → 任务状态 → complete
```

### Danbooru 参考流程
```
管理员触发 Danbooru 画师导入
  → 拉取 Danbooru 画师页面
  → 解析画师标签、关联 URL
  → 创建 creator_link 建议（状态=待审核）
  → 管理员审核：通过 → 绑定到 creator，拒绝 → 丢弃
```

## 可靠性与恢复

由于 gallery-dl 直接写盘，且下载 → 导入交接跨越两个队列，系统提供若干恢复路径让数据库与磁盘实际内容保持一致。它们全部幂等，绝不删除或重新下载。

- **磁盘导入**（`services/disk_import.py`，`POST /api/v1/admin/library/import-from-disk`）：扫描 `DOWNLOAD_ROOT/{source}/`，把磁盘上存在但从未入库（账本状态 ≠ `done`）的文件以一个合成 `recovery` download_job 导入。`services/disk_identity.py` 会先从本地元数据补建真实的 `creator → subscription → subscription_source → source_creator` 链（Danbooru 富化为尽力而为，并带纯元数据兜底），使恢复的作品挂到正确创作者。运行在 `operations` 队列；Redis 锁（`library:disk-import:active`）防止并发运行。
- **导入管线恢复**（`services/import_recovery.py`）：对安全缺口的保守式自动修复——例如为已有待处理元数据的 `downloaded` 任务重新入队导入。只读健康状态由 `services/pipeline_health.py` 报告。
- **创作者自动重链**（`services/creator_reconcile.py`）：每次订阅同步扫描时，按身份匹配把孤立的 `source_creators`（`creator_id IS NULL`）链接到其订阅的创作者。它沿用 `import_runner` 的自动链接规则，因此身份模糊的收藏/转发源绝不会被错链。
- **Worker 启动孤儿对账**：启动时把卡在 `downloading`/`importing` 的任务标记为 `stale`，并把磁盘上无对应任务的目录重新拾取导入。

## Gitllery —— 磁盘上的策展历史

一个单向、与 git 同构的投影，把权威的策展 DAG（`curation_commits` + `curation_changes`，以及各 `*_curation_state` 表）按每个 source-creator 投影到磁盘。**PostgreSQL 始终是权威数据源**——磁盘仓库只是一个面向读取优化的镜像，绝不是回写数据库的路径。每次策展提交（如 `purge`、`revert`、`curate_creator`）之后，以尽力而为（best-effort）、同步的方式写入；失败会被吞掉并记录日志（`project_commit_safe`），因此磁盘写入的偶发故障绝不会破坏策展 API 的响应。追赶投影（catch-up）和校验机制负责补齐任何缺口。

- **目录结构**：`LIBRARY_ROOT/{source}/{creator_dir}/.gitllery/` —— 与该创作者既有的 library 文件同级，绝不是 library 根目录本身（路径包含关系通过 `Path.relative_to` 校验，与 `LIBRARY_ROOT` 下其他规则一致）。包含：
  - `HEAD` —— 符号引用，固定为 `ref: refs/heads/main`
  - `refs/heads/main` —— 当前提交哈希
  - `objects/{aa}/{rest}` —— 内容寻址对象库，采用 git 风格的 2 字符分片前缀
  - `logs/HEAD` —— 追加写入的引用日志（reflog，每次 `set_head` 写一行 JSON：旧/新哈希、actor、时间戳、message）
  - `index.json` —— 物化的工作区状态，用于快速状态检查（`head`、`tree`、`tree_entries`、`entities`）
  - `config.json` —— 仓库元数据（`schema_version`、`repository_id`、`source`、`creator_id`、`source_creator_id`、`creator_dir`、哈希/压缩算法）
  - `description` —— 初始化时写入的可读标签

- **对象模型**：每个对象都是规范化 JSON（按 key 排序、紧凑分隔符）经 zlib 压缩后存储，以**未压缩**规范字节的 sha256 寻址。
  - **blob** —— 单个实体状态快照（`subject_type`、`subject_id`、`state`）
  - **tree** —— 工作区清单：`{subject_type}/{subject_id}` → blob 哈希 的映射
  - **commit** —— 一次被投影的策展提交：`tree` 哈希、仓库本地的 `parent` 哈希（链表结构，并非数据库提交的 parent）、`db_commit_id`（关联回 `curation_commits.id`）、actor/message/trigger/dedupe_key/reverts/occurred_at/stats，以及本次应用的完整变更列表

- **操作**（`GitlleryService`，`backend/app/services/gitllery/`）：
  - **`status`** —— 每个仓库的 `behind`（尚未投影的待处理 DB 提交数）、`object_integrity_ok`（从 HEAD 经 tree 到 blob 的哈希链校验）、`drift`（磁盘快照与当前 `*_curation_state` 不一致的实体，例如 `visibility`/`storage_state` 在投影之外被修改）
  - **`reconcile`** —— 对缺失的提交运行追赶投影（`project_pending`），然后返回刷新后的 `status`
  - **`backfill`** —— 确保每个 source-creator 仓库存在，然后从头投影整个策展 DAG（用于首次同步 / 历史数据回填）
  - 同一仓库的写入通过 Redis 锁（`gitllery:{repository_id}`）串行化，避免并发投影在同一个 `.gitllery` 目录上产生竞态

- **API**：所有端点都挂在策展路由下（需要 `RequireAdmin`），路径为 `/api/v1/curation/gitllery/...`：
  - `GET /gitllery/status`、`GET /repositories/{repository_id}/gitllery/status`
  - `POST /gitllery/reconcile`、`POST /gitllery/backfill`
  - `GET /repositories/{repository_id}/gitllery/log`

  管理端的 **GitlleryPanel** 组件展示干净/落后状态，并提供一键 reconcile 操作。

## 认证

管理端和后端 API 使用基于 JWT 的认证：

- **登录**：POST `/api/v1/auth/login` 使用用户名/密码返回 access token。
- **Token 格式**：使用服务器密钥签名的 JWT access token。
- **认证方式**：管理端点要求 `Authorization: Bearer <jwt>`。
- **Token 过期**：通过 `ACCESS_TOKEN_EXPIRE_MINUTES` 配置（默认 30 分钟）。
- **首次登录轮换**：引导创建的管理员账户会标记 `must_change_password=true`，完成改密后才能继续访问管理接口。
- 所有管理 API 路由需要通过 `RequireAdmin` 依赖进行认证。

## 备份系统

系统包含自动备份能力：

- **触发**：POST `/api/v1/admin/backup` 创建完整备份（pg_dump + 配置 tar + 下载归档列表）。
- **格式**：压缩 tar.gz 归档，存储在 `DOWNLOAD_ROOT/.backups/` 下。
- **下载**：GET `/api/v1/admin/backup/download` 提供备份文件下载。
- **列表**：GET `/api/v1/admin/backup` 列出可用备份。
- **恢复**：POST `/api/v1/admin/backup/restore` 从备份归档恢复。
- **调度**：POST `/api/v1/admin/backup/schedule` 启用/禁用通过 RQ 调度器的定时自动备份，间隔可配置。旧备份自动清理（保留最近 5 个）。

## 完整性检查

完整性验证系统扫描数据一致性问题：

- **端点**：GET `/api/v1/admin/integrity-check`
- **执行的检查**：
  - `downloads/` 中无对应数据库记录的孤立文件
  - 应存在但缺失的缩略图
  - 指向不存在文件的孤立数据库记录
  - 作品来源 / 资源来源一致性

## 容器路径

应用代码仅使用这些由环境变量驱动的路径：

| 变量 | 容器路径 | 用途 |
|---|---|---|
| `DOWNLOAD_ROOT` | `/downloads` | 原图仓库：长期保存原始文件，按 source/creator/work 组织 |
| `LIBRARY_ROOT` | `/library` | 索引层：每个作品的元数据 + 缩略图 |
| `GALLERYDL_CONFIG_ROOT` | `/gallerydl-config` | gallery-dl 配置、cookie、短生命周期任务配置 |
| `APP_CONFIG_ROOT` | `/app-config` | 应用运行时配置 |

NAS 主机路径仅在 `docker-compose.yaml` 中映射。

## NAS 存储结构

```
/volume1/auto-gallery/
├── downloads/                          # DOWNLOAD_ROOT — 原图仓库
│   └── {source}/                       # 如 pixiv, iwara, x
│       └── {creator_name}/             # 如 ASK, 1980643
│           └── {source_work_id}/        # 如 38362603（Pixiv 作品 ID）
│               ├── 38362603_p0.jpg     # 第 0 页
│               └── 38362603_p1.jpg     # 第 1 页（多页时）
│
├── library/                            # LIBRARY_ROOT — 索引层
│   └── {source}/                       # 如 pixiv, iwara, x
│       └── {creator_name}/             # 如 ASK, 1980643
│           └── {source_work_id}/        # 与 downloads 相同的 ID — 链接两者
│               ├── metadata.json       # 作品元数据导出
│               └── thumbnail.webp      # 400px WebP 缩略图（pyvips）
│
├── config/
│   ├── gallery-dl/                     # GALLERYDL_CONFIG_ROOT
│   │   ├── config.json                 # gallery-dl 基础配置
│   │   ├── cookies/                    # 各来源认证 cookie
│   │   └── jobs/                       # worker 短生命周期任务配置（自动清理）
│   └── app/                            # APP_CONFIG_ROOT — 运行时配置
│
├── docker/                             # 持久化卷
│   ├── postgres/                       # PostgreSQL 数据
│   ├── redis/                          # Redis 数据
│   └── meilisearch/                    # Meilisearch 数据
│
└── backup/                             # 备份
    ├── db/                             # PostgreSQL 转储
    ├── config/                         # 配置备份
    └── metadata/                       # metadata.json 副本
```

### 存储规则

- **downloads/**：原图仓库。原始图片/视频文件按 `{source}/{creator_name}/{source_work_id}/` 组织并长期保存。导入时从 gallery-dl 的扁平输出移动至此。JSON 元数据文件处理后被删除。路径中不包含 job_id 层级。
- **library/**：索引层。仅每个作品的元数据 + 缩略图。与 downloads 使用相同的 `{source}/{creator_name}/{source_work_id}/` 结构，通过 source_work_id 链接。不存放原始图片。
- **gallery-dl 输出**：导入前的 worker 短生命周期输出。导入过程中文件被重组到长期保存的原图仓库中，JSON 文件被删除。
- **缩略图**：400px WebP 格式，由 pyvips 从首页图片生成。从 LIBRARY_ROOT 通过无鉴权的 `/media/thumb/{asset_id}` 提供，用于网格/列表中的图片标签。
- **预览/原图**：直接从 DOWNLOAD_ROOT 通过 `/media/preview/{asset_id}` 和 `/media/original/{asset_id}` 提供。预览需要短期签名 URL；原图接受管理员 Bearer 鉴权或短期签名 URL。
- **metadata.json**：导入时为每个作品写入。包含 work_id、source、source_work_id、title、posted_at、creator、assets 数组。
- **source_work_id**：平台特定的作品 ID（如 Pixiv 作品 ID "38362603"）。在 downloads/ 和 library/ 中都用作目录名。链接两个存储树。

## 环境变量

应用使用的关键环境变量：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `BACKEND_PORT` | `8818` | 后端 API 主机端口（映射容器 8000） |
| `ADMIN_WEB_PORT` | `13000` | 管理端主机端口（映射容器 3000） |
| `CORS_ORIGINS` | `http://localhost:13000` | 允许的 CORS 来源 |
| `BACKEND_INTERNAL_URL` | `http://backend:8000` | admin-web SSR 访问后端的内部 URL |
| `NEXT_PUBLIC_WS_URL` | 当前站点 `/api/v1/ws` | 浏览器连接实时任务 WebSocket 的公开地址 |
| `DATABASE_URL` | （必需） | PostgreSQL 连接字符串 |
| `REDIS_URL` | （必需） | Redis 连接字符串 |
| `SECRET_KEY` | （必需） | JWT 签名密钥 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | JWT token 过期时间 |
| `TIMEZONE` | `UTC` | 调度器和显示的时区 |
| `DOWNLOAD_ROOT` | `/downloads` | 原始文件存储路径 |
| `LIBRARY_ROOT` | `/library` | 元数据 + 缩略图存储路径 |
| `GALLERYDL_CONFIG_ROOT` | `/gallerydl-config` | gallery-dl 配置路径 |
| `APP_CONFIG_ROOT` | `/app-config` | 应用配置路径 |

## API 路由分组

所有路由均以 `/api/v1` 为前缀，除非另有说明。

### `/api/v1/system`
健康检查、队列统计、存储统计、清除失败任务、日志缓冲。

端点：
- `GET /health` — 系统健康检查（数据库、Redis、Meilisearch、磁盘）
- `GET /health/disk` — 磁盘使用统计
- `GET /queue-stats` — 等待/运行/失败任务数量
- `GET /storage-breakdown` — 各来源存储使用
- `POST /clear-failed-jobs` — 清除所有失败的下载/导入任务
- `GET /logs` — 查看缓冲日志输出

### `/api/v1/admin`
设置、gallery-dl 配置、系统信息、完整性、备份、导入进度、代理测试、调度器控制、去重、合并候选、重建索引、数据清理。

端点包括：
- `GET /settings` / `PUT /settings` — 系统设置 CRUD
- `POST /settings/reset` — 重置设置为默认值
- `GET /gallerydl-config` / `PUT /gallerydl-config` — 各来源 gallery-dl 提取器配置
- `GET /system-info` — 系统概览（CPU、内存、磁盘、数据库大小、归档大小）
- `GET /integrity-check` — 扫描孤立文件、缺失缩略图、孤立记录
- `POST /backup` — 创建完整系统备份（pg_dump + 配置 + 归档）
- `GET /backup` — 列出可用备份
- `GET /backup/download` — 下载备份文件
- `POST /backup/restore` — 从备份归档恢复
- `POST /backup/schedule` — 启用/禁用定时自动备份
- `POST /cleanup-metadata-jsons` — 删除过期 JSON 元数据文件
- `GET /import-progress` — 实时导入进度轮询
- `POST /proxy/test` — 测试代理与 gallery-dl 连接
- `POST /scheduler/sync-now` — 立即触发订阅同步
- `POST /library/import-from-disk` — 幂等地把磁盘上的下载文件入库，无需重新下载（operations 队列）
- `POST /clear/{entity}` — 清除指定实体类型的所有记录（`jobs`/`all` 同时清除 `task_runs`）
- `GET /dedup` / `PUT /dedup` — 去重设置
- `GET /merge-candidates` — 已废弃的兼容别名，返回待审核图片资产候选；新客户端使用 `/api/v1/admin/dedup/cases`
- `POST /reindex` — 触发 Meilisearch 全量重建索引

### `/api/v1/creators`
创作者 CRUD、计数、时间线、重复检测、合并、收藏、来源账号绑定、创作者链接。

端点包括：
- `GET /` — 列出创作者（分页、可搜索）
- `POST /` — 创建创作者
- `GET /count` — 创作者总数
- `GET /{id}` — 创作者详情（含来源账号和链接）
- `PUT /{id}` — 更新创作者
- `DELETE /{id}` — 删除创作者
- `GET /{id}/timeline` — 发布活动时间线
- `GET /duplicates` — 检测潜在的重复创作者
- `POST /merge` — 合并两个创作者
- `POST /{id}/favorite` / `DELETE /{id}/favorite` — 切换收藏
- `POST /{id}/source-accounts` — 绑定 source_creator 到此创作者
- `DELETE /{id}/source-accounts/{sc_id}` — 解绑 source_creator
- `GET /{id}/links` / `POST /{id}/links` — 管理 creator_url 链接
- `PUT /links/{link_id}` — 更新链接（通过/拒绝）

### `/api/v1/subscriptions`
订阅 CRUD、来源切换、批量操作、同步触发。

端点包括：
- `GET /` — 列出订阅
- `POST /` — 创建订阅
- `GET /count` — 订阅总数
- `GET /{id}` — 订阅详情（含来源）
- `PUT /{id}` — 更新订阅
- `DELETE /{id}` — 删除订阅
- `POST /batch-delete` — 批量删除订阅
- `POST /batch-toggle-sync` — 批量切换同步状态
- `POST /{id}/sync-now` — 立即同步此订阅
- `GET /{id}/sources` — 列出订阅的来源
- `PUT /{id}/sources/{source}` — 启用/禁用/切换订阅来源

### `/api/v1/download-jobs`
下载任务队列管理，支持批量操作和逐任务控制。

端点包括：
- `GET /` — 列出下载任务（分页、可按状态筛选）
- `POST /` — 创建下载任务
- `GET /{id}` — 任务详情
- `DELETE /{id}` — 删除任务
- `POST /clear` — 清除所有已完成/失败任务
- `POST /kill-stuck` — 终止所有卡住的任务（僵死检测）
- `POST /retry-all` — 重试所有失败任务
- `POST /batch-delete` — 批量删除任务
- `POST /{id}/pause` — 暂停任务
- `POST /{id}/resume` — 恢复暂停的任务
- `POST /{id}/retry` — 重试特定失败任务
- `POST /{id}/cancel` — 取消任务（终态，保留记录）— TaskEngine
- `POST /{id}/priority` — 调整队列优先级 — TaskEngine
- `POST /batch-by-filter` — 对匹配筛选条件的所有任务执行操作 — TaskEngine
- `GET /{id}/progress` — 实时进度快照
- `GET /{id}/pipeline` — 逐阶段管线视图

### `/api/v1/import-jobs`
导入任务队列和导入后扫描。列表条目会用父下载任务的上下文（来源、URL、订阅/创作者）富化，使其与下载列表对齐。

端点包括：
- `GET /` — 列出导入任务（富化来源/订阅/创作者）
- `GET /{id}` — 导入任务详情（含错误信息）
- `POST /{id}/retry` — 重试失败的导入
- `POST /{id}/cancel` — 取消导入 — TaskEngine
- `POST /{id}/priority` — 调整队列优先级 — TaskEngine
- `POST /batch-by-filter` — 对匹配筛选条件的所有导入执行操作 — TaskEngine
- `GET /{id}/progress` — 实时进度快照
- `POST /scan` — 扫描未导入的下载并创建导入任务

### `/api/v1/works`
作品浏览，支持批量操作、标签和来源详情。

端点包括：
- `GET /` — 列出作品（分页、可筛选）
- `GET /{id}` — 作品详情
- `PUT /{id}` — 更新作品
- `DELETE /{id}` — 删除作品
- `POST /batch-delete` — 批量删除作品
- `POST /batch-tag` — 批量添加/移除标签
- `GET /{id}/assets` — 列出作品的资源
- `GET /{id}/sources` — 列出作品的 work_sources
- `GET /{id}/tags` — 列出作品的标签
- `POST /{id}/favorite` / `DELETE /{id}/favorite` — 切换收藏

### `/api/v1/tags`
标签管理，支持别名和合并。

端点包括：
- `GET /` — 列出标签
- `POST /` — 创建标签
- `GET /{id}` — 标签详情
- `PUT /{id}` — 更新标签
- `DELETE /{id}` — 删除标签
- `POST /{id}/alias` — 设置标签别名
- `POST /merge` — 合并两个标签

### `/api/v1/search`
基于 Meilisearch 的全文搜索和索引管理。

端点包括：
- `GET /` — 搜索作品、创作者、标签
- `POST /reindex` — 触发 Meilisearch 全量重建索引

### `/api/v1/sources`
来源 provider 列表和能力。

端点：
- `GET /` — 列出所有已注册的来源 provider 及其能力
- `GET /{source}` — 特定 provider 的能力

### `/api/v1/reference`
Danbooru 参考 provider 操作，用于创作者身份映射。

端点包括：
- `POST /danbooru/preview` — 预览 Danbooru 画师标签结果
- `POST /danbooru/import` — 导入单个 Danbooru 画师参考
- `POST /danbooru/batch-import` — 从 Danbooru 批量导入

### `/api/v1/auth`
认证端点。

端点：
- `POST /login` — 登录，返回 JWT access token
- `GET /me` — 当前认证用户信息
- `POST /change-password` — 更改当前用户密码


### `/media`（无版本号）
受控媒体文件服务。不以 `/api/v1` 为前缀。

端点：
- `GET /thumb/{asset_id}` — 来自 LIBRARY_ROOT 的 400px WebP 缩略图
- `GET /preview/{asset_id}` — 来自 DOWNLOAD_ROOT 的原始文件，需要短期签名 URL
- `GET /original/{asset_id}` — 来自 DOWNLOAD_ROOT 的原始文件（下载用）
