# 系统架构

## 概述

auto-gallery 是一个分层的 Docker Compose 应用，从多个来源下载媒体，将元数据导入规范的 PostgreSQL 数据库，并通过 FastAPI 后端和 Next.js 管理界面提供服务。

## 分层图

```
┌─────────────────────────────────────────────────┐
│  Admin Web (Next.js)                             │
│  /src/app/*  /src/components/*  TanStack Query   │
└──────────────────┬──────────────────────────────┘
                   │ HTTP REST
┌──────────────────▼──────────────────────────────┐
│  Backend (FastAPI)                               │
│  Routes → Services → Repositories → SQLAlchemy   │
│  Providers: Pixiv, Iwara, X, Danbooru, local    │
└──────┬────────────┬──────────────┬──────────────┘
       │            │              │
       │   ┌────────▼────────┐     │
       │   │  Worker (RQ)    │     │
       │   │  gallery-dl     │     │
       │   │  导入管线       │     │
       │   └────────┬────────┘     │
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

所有模型使用与来源无关的命名：

```
creator ──< source_creator
creator ──< creator_link
creator ──< subscription ──< subscription_source
work ──< work_source
work ──< work_tag
work_source ──< work_source_tag
asset ──< asset_source
tag
naming_template
download_job
import_job
```

### 关键关系

- **creator**：规范本地身份（如"画师 A"）
- **source_creator**：平台特定账号（如 Pixiv 用户 123456）
- **creator_link**：链接创作者到外部个人资料的 URL
- **work**：规范本地作品（可有多个 work_source）
- **work_source**：来源特定的作品记录，保留原始元数据
- **asset**：本地文件
- **asset_source**：来源特定的文件记录
- **subscription**：用户追踪创作者的意图
- **subscription_source**：每个订阅的逐来源开关

### 唯一性约束

- `(source, source_creator_id)` 在 source_creators 表
- `(source, source_work_id)` 在 work_sources 表
- `(source, source_asset_id)` 在 asset_sources 表
- `(normalized_name)` 在 tags 表
- `(creator_id)` 在 subscriptions 表
- `(subscription_id, source)` 在 subscription_sources 表

## Provider 系统

来源特定行为封装在 `backend/app/providers/` 下的 provider 模块中。

每个 provider 实现：
- URL 规范化和验证
- gallery-dl 配置生成（如果可下载）
- 元数据解析（source_creator、work_source、asset、tag）

Provider 绝不访问数据库。它们接收原始元数据字典并返回数据字典。服务层负责持久化。

## 数据流

### 下载流程
```
Scheduler → 创建 download_job（pending）
  → Worker 取走任务（downloading）
    → Provider.build_gallerydl_config()
    → subprocess.run(["gallery-dl", ...], shell=False)
    → 文件落盘到 DOWNLOAD_ROOT/<job_id>/
    → 任务状态 → downloaded
    → 入队 import_job
```

### 导入流程
```
Worker 取走 import_job
  → 读取 gallery-dl 输出的 .info.json
  → Provider.parse_source_creator() → upsert source_creator
  → Provider.parse_work_source() → upsert work_source
  → Provider.parse_assets() → upsert asset + asset_source
  → Provider.parse_source_tags() → upsert tags + work_source_tags
  → 文件从 DOWNLOAD_ROOT 移动到 LIBRARY_ROOT
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

## 容器路径

应用代码仅使用这些由环境变量驱动的路径：

| 变量 | 容器路径 | 用途 |
|---|---|---|
| `DOWNLOAD_ROOT` | `/downloads` | gallery-dl 下载暂存 |
| `LIBRARY_ROOT` | `/library` | 组织化媒体库 |
| `GALLERYDL_CONFIG_ROOT` | `/gallerydl-config` | gallery-dl 配置、cookie、临时任务配置 |
| `APP_CONFIG_ROOT` | `/app-config` | 应用运行时配置 |

NAS 主机路径仅在 `docker-compose.yml` 中映射。

## API 路由分组

```
/api/v1/system          健康检查、版本、统计
/api/v1/sources         列出来源及其能力
/api/v1/reference       Danbooru 参考操作
/api/v1/creators        创作者 CRUD、来源账号绑定
/api/v1/creator-links   链接管理、审核
/api/v1/subscriptions   订阅 CRUD、来源选择
/api/v1/download-jobs   下载队列、重试、取消
/api/v1/import-jobs     导入状态、错误
/api/v1/works           作品浏览、作品来源详情
/api/v1/assets          资源列表、元数据
/api/v1/tags            标签管理、来源标签查看
/api/v1/search          Meilisearch 查询
/api/v1/admin           管理员设置、去重配置、合并候选
/media                  受控媒体文件服务（无版本号）
```
