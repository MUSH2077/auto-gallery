# 安装指南

## 前置条件

- Docker Engine 24+
- Docker Compose v2
- Linux 主机（NAS 或服务器），位于本地网络
- 足够存储媒体库的磁盘空间

## 服务端口

| 服务      | 主机端口 | 容器端口 |
|-----------|----------|----------|
| backend   | 8818     | 8000     |
| admin-web | 13000    | 3000     |

两个端口均可通过 `.env` 中的 `BACKEND_PORT` 和 `ADMIN_WEB_PORT` 配置。

## 安装步骤

### 1. 目录结构

在 NAS 上创建 auto-gallery 目录：

```bash
mkdir -p /volume1/auto-gallery/{downloads,library,config/{app,gallery-dl/{cookies,jobs}},docker/{postgres,redis,meilisearch}}
cd /volume1/auto-gallery
```

### 2. 配置

```bash
scripts/generate-env.sh
```

脚本会生成带随机服务密钥的 `.env`。如果 `.env` 已存在，默认不会覆盖；需要重建时可运行 `scripts/generate-env.sh --force`。

如果你希望手动配置密钥，也可以复制 `.env.example` 为 `.env`，并替换：

```bash
# 为每个服务生成强密码；这些值不能保留 change-me-* 占位符
POSTGRES_PASSWORD=<生成>
REDIS_PASSWORD=<生成>
MEILI_MASTER_KEY=<生成>
SECRET_KEY=<生成>

# 可保留 change-me-admin 作为首次登录密码，也可改成自定义初始密码
ADMIN_PASSWORD=change-me-admin
```

首次登录管理后台使用 `admin / change-me-admin`。登录后系统会强制你修改密码；如果部署前已把 `ADMIN_PASSWORD` 改成自定义值，则使用该自定义值首次登录。

如果 backend 日志出现 `auto-gallery refused to start — insecure defaults detected`，请确认 Docker Compose 读取的是正确的 `.env`、上面的服务密钥已经不再是 `change-me-*`，并且更新代码后已重新 build backend 镜像。

设置时区：

```bash
# 例如 Asia/Shanghai, America/New_York, UTC
TIMEZONE=Asia/Shanghai
```

本地开发使用 `.env.example` 中的默认开发路径即可。NAS 部署需设置主机路径：

```bash
# NAS 主机路径（在 .env 中，由 docker-compose.yaml 引用）
HOST_DOWNLOADS=/volume1/auto-gallery/downloads
HOST_LIBRARY=/volume1/auto-gallery/library
HOST_CONFIG_APP=/volume1/auto-gallery/config/app
HOST_CONFIG_GALLERYDL=/volume1/auto-gallery/config/gallery-dl
HOST_POSTGRES=/volume1/auto-gallery/docker/postgres
HOST_REDIS=/volume1/auto-gallery/docker/redis
HOST_MEILISEARCH=/volume1/auto-gallery/docker/meilisearch
```

### 3. 启动全部服务

```bash
docker compose up -d
```

此命令启动全部服务：postgres、redis、meilisearch、backend、worker、scheduler 和 admin-web。后端启动时会自动执行数据库迁移，无需单独运行迁移步骤。

等待健康检查通过：

```bash
docker compose ps
# 全部服务均应显示 "healthy"
```

如果仅需启动基础设施服务用于本地开发：

```bash
docker compose up -d postgres redis meilisearch
```

### 4. 验证

```bash
# 健康检查
curl http://localhost:8818/api/v1/system/health

# 期望响应：
# {"status":"ok","services":{"postgres":"up","redis":"up","meilisearch":"up"}}
```

管理后台：`http://<主机IP>:13000`。

## gallery-dl 配置

### Pixiv 认证

1. 在浏览器中登录 Pixiv
2. 用浏览器扩展（如"Export Cookies"）导出 cookie
3. 将 cookie 文件放到 `config/gallery-dl/cookies/pixiv.txt`
4. `config/gallery-dl/config.json` 中引用：

```json
{
  "extractor": {
    "pixiv": {
      "cookies": "/gallerydl-config/cookies/pixiv.txt"
    }
  }
}
```

### 通过管理后台按来源配置

每个来源（Pixiv、X/Twitter、Iwara、Danbooru、Pinterest、LOFTER、微博、Bilibili）均可通过管理后台 **设置 > gallery-dl 配置** 进行配置，包括：

- 认证（Cookie、Refresh Token、API 密钥、用户名/密码）
- 内容过滤（作品、收藏、书签、推文、喜欢）
- 标签语言偏好
- Ugoira 格式（ZIP 或 GIF）
- 目录与文件名模式
- 速率限制（请求间隔）
- 单次最大帖子数
- 视频画质偏好
- 导入时默认启用（按来源）

修改会自动保存到 `config.json`。建议通过管理后台配置 gallery-dl 提取器；手动编辑 `config.json` 亦可作为备选方式。

### 命名模板

命名模板控制文件在 `DOWNLOAD_ROOT` 和 `LIBRARY_ROOT` 中的组织方式。使用 gallery-dl 的模板语法（例如 `pixiv/{user[account]}/{id}`）。可通过以下方式管理：

- **管理后台**：**设置 > 命名模板** -- 按来源创建、编辑和设置默认模板
- **数据库**：存储在 `naming_template` 表中

## 备份与恢复

系统内建备份与恢复功能，可通过管理后台 **设置 > 备份与恢复** 访问。备份内容包括：

- PostgreSQL 数据库（创作者、订阅、作品、标签、设置、任务历史）
- gallery-dl 配置（提取器设置、Cookie、认证令牌）
- 应用配置（命名模板等）
- 下载归档（archive-*.sqlite3，用于防止重复下载）

可手动创建备份并下载至本地保存。系统每 24 小时自动创建一次备份。恢复功能可上传备份文件并替换当前系统状态。

## 开发环境

本地开发（无需 NAS）：

```bash
# 使用默认的 .env.example 路径（本地目录）
cp .env.example .env

# 创建本地数据目录
mkdir -p data/{downloads,library,config/{app,gallery-dl/{cookies,jobs}}}

# 启动服务
docker compose up -d
```

## 常见问题

### worker 中找不到 gallery-dl

确认后端镜像已安装 gallery-dl。检查 `backend/requirements.txt` 是否包含 `gallery-dl`。

### 数据库连接被拒绝

PostgreSQL 可能在后端启动时尚未就绪。Docker Compose 的 `depends_on` 配合 `condition: service_healthy` 可解决此问题。

### 卷权限被拒绝

确认 Docker 用户（通常 uid 1000）对主机目录有写权限。在 Synology NAS 上可能需要通过 DSM File Station 设置权限。

### Meilisearch master key 不匹配

`.env` 中的 `MEILI_MASTER_KEY` 必须与后端使用的密钥一致。如果在 Meilisearch 启动后更改，需删除 `docker/meilisearch/` 数据目录后重启。
