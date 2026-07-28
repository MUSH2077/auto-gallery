# auto-gallery

[English](README.md)

[![CI](https://github.com/MUSH2077/auto-gallery/actions/workflows/ci.yml/badge.svg)](https://github.com/MUSH2077/auto-gallery/actions/workflows/ci.yml)
[![CodeQL](https://github.com/MUSH2077/auto-gallery/actions/workflows/codeql.yml/badge.svg)](https://github.com/MUSH2077/auto-gallery/actions/workflows/codeql.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/MUSH2077/auto-gallery/badge)](https://securityscorecards.dev/viewer/?uri=github.com/MUSH2077/auto-gallery)
[![License: AGPL-3.0-only](https://img.shields.io/badge/license-AGPL--3.0--only-blue)](LICENSE)
[![Status: pre-1.0 beta](https://img.shields.io/badge/status-pre--1.0%20beta-f59e0b)](CHANGELOG.md)

面向创作者内容集合的自托管媒体归档与策展系统。

auto-gallery 通过 Docker Compose 从多个来源下载、导入、索引并管理媒体，适用于个人
NAS 和 Linux 主机。响应式管理工作台覆盖创作者、来源仓库、作品、标签、订阅、队列、
调度、搜索、存储、策展历史和 gallery-dl 配置。

> [!WARNING]
> auto-gallery 目前是面向可信自托管环境的 1.0 前 Beta。不要把管理后台或后端 API
> 直接暴露到公网；请使用反向代理、TLS、访问控制，并根据你的网络环境建立威胁模型。

## 为什么选择 auto-gallery？

- **以创作者为中心。** 一个规范创作者可关联多个平台身份和来源仓库。
- **仓库化同步。** 每个受支持的 gallery-dl 订阅 URL 都是可观测、可独立调度的仓库，
  拥有自己的任务、标签、作品、健康状态与历史。
- **保留原始媒体。** 下载文件保持为事实来源，PostgreSQL、缩略图与 Meilisearch
  共同提供可浏览的媒体库索引。
- **可靠后台任务。** 下载、导入、恢复、备份和维护在独立 worker 中执行，并提供队列、
  调度、健康和存储可见性。
- **可审计策展。** 可逆的策展提交会投影为内容寻址的 `.gitllery` 历史，用于完整性校验和恢复。
- **跨来源资产去重。** 仅比较不同来源、不同 Work 的图片；视觉证据是硬门槛，创作者身份和
  发布时间可进一步加分，歧义候选进入人工审核。
- **可视化浏览。** 通过响应式作品网格、创作者存储树、标签气泡、搜索、幻灯片和可选展示页
  浏览大规模收藏。
- **可访问的管理体验。** 支持中英文、模块权限、键盘导航、触屏目标、响应式布局、深色模式
  与降低动效。

## 截图

![使用虚构运维数据的当前管理仪表盘](docs/assets/admin-dashboard.png)

| 标签分布 | 跨来源图片资产审核 |
|---|---|
| ![使用虚构标签的当前标签气泡图](docs/assets/tag-bubbles.png) | ![使用生成式几何占位图的当前图片资产去重审核页](docs/assets/asset-dedup-review.png) |

这些截图由当前管理前端配合拦截式虚构数据和程序生成的几何占位图渲染，不包含凭据、
本地路径、真实创作者或已下载媒体。

## 支持的来源

Provider 兼容性依赖 gallery-dl，目标站点变化可能造成兼容性变化。当前限制请查阅
[Provider 指南](docs/providers.zh.md)。

| 来源 | 下载方式 | 认证 | 状态 |
|---|---|---|---|
| Pixiv | gallery-dl | Refresh token 或 cookies | 已支持 |
| X/Twitter | gallery-dl `twitter` extractor | Cookies | 已支持 |
| Iwara | gallery-dl | 用户名/密码或 cookies | 已支持 |
| Danbooru | gallery-dl | API key、用户名/密码或 cookies | 已支持 |
| Weibo | gallery-dl | 可选 cookies | 已支持 |
| Bilibili | gallery-dl | 公开内容 | 已支持 |
| Pinterest | gallery-dl | 公开内容 | 已支持 |
| LOFTER | gallery-dl | 公开内容 | 已支持 |
| 手动上传 | 管理后台 | 管理员 | 已支持 |
| 本地文件夹 | 直接导入 | 本地路径 | 规划中 |

## 快速开始

### 环境要求

- Docker Engine 与 Docker Compose v2
- 带持久化存储的 Linux 主机或 NAS
- 能容纳原始媒体、缩略图和备份的磁盘空间

```bash
git clone https://github.com/MUSH2077/auto-gallery.git
cd auto-gallery

bash scripts/generate-env.sh
# 检查 .env，特别是宿主机路径、端口、时区与 ADMIN_PASSWORD。

docker compose up -d --build
curl http://localhost:8818/api/v1/system/health
```

backend 启动时会自动运行数据库迁移。访问 `http://<host>:13000`，立即修改初始密码，
再到设置中配置 gallery-dl 凭据和文件组织规则。

反向代理、存储布局、升级与故障排查请参阅[完整部署指南](docs/setup.zh.md)。

## 架构

```text
Sources
  -> gallery-dl download workers / manual upload
    -> Original Media Store (DOWNLOAD_ROOT)
      -> import and asset processing
        -> Library Index (metadata + thumbnails)
        -> PostgreSQL canonical data
        -> Meilisearch full-text index
        -> Next.js admin web
```

核心模型与来源无关：创作者、作品、资产、标签和订阅来源。Provider 模块负责 URL
校验与规范化、gallery-dl 配置和元数据解析。策展变更以 PostgreSQL 为事实来源并投影到
每个仓库的 `.gitllery` 历史；资产对账会保留 Work 和来源记录，只选择视觉展示代表。
修改这些边界前请阅读[架构文档](docs/architecture.zh.md)。

## 文档

- [文档索引](docs/README.md)
- [部署与升级](docs/setup.zh.md)
- [架构](docs/architecture.zh.md)
- [Provider 开发](docs/providers.zh.md)
- [开发指南](docs/development.zh.md)
- [安全策略](SECURITY.md)
- [贡献指南](CONTRIBUTING.zh.md)
- [项目治理](GOVERNANCE.md)
- [支持渠道](SUPPORT.md)
- [变更记录](CHANGELOG.md)

## 项目状态

auto-gallery 仍处于 1.0 之前。数据库迁移、Provider 行为和部署设置可能在 Beta
版本之间变化，升级前请备份媒体与应用状态。

已知限制：

- 来源站点可能随时变化并导致 gallery-dl extractor 失效。
- Cookies 和平台凭据需要运维者持续维护。
- 大规模首次同步可能在 NAS 上消耗较长时间和较高磁盘 I/O。
- 搜索索引可能短暂滞后于 PostgreSQL。
- 产品面向管理员，不是公开的多租户媒体服务。

近期规划通过
[GitHub issues](https://github.com/MUSH2077/auto-gallery/issues) 跟踪，重点是 Provider
兼容性 fixture、恢复演练、本地文件夹导入、公网部署加固和首个稳定 Beta 标签。

## 合法与负责任使用

仅使用 auto-gallery 归档你有权访问和下载的内容。你需要自行遵守来源平台条款、
版权法律和所在地法律。

本项目不鼓励绕过付费墙、DRM、访问控制、速率限制或平台限制。不要在 Issue 或 PR
中公开 cookies、凭据、私有创作者 URL、私有媒体、数据库备份或未脱敏日志。

## 社区与许可证

参与前请阅读[贡献指南](CONTRIBUTING.zh.md)和
[行为准则](CODE_OF_CONDUCT.md)。安全问题必须按 [SECURITY.md](SECURITY.md)
中的私密流程报告。

auto-gallery 使用
[GNU Affero General Public License v3.0 only](LICENSE)。
