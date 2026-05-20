# 安装指南

## 前置条件

- Docker Engine 24+
- Docker Compose v2
- Linux 主机（NAS 或服务器），位于本地网络
- 足够存储媒体库的磁盘空间

## 安装步骤

### 1. 目录结构

在 NAS 上创建 auto-gallery 目录：

```bash
mkdir -p /volume1/auto-gallery/{downloads,library,config/{app,gallery-dl/{cookies,jobs}},docker/{postgres,redis,meilisearch}}
cd /volume1/auto-gallery
```

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`，设置：

```bash
# 为每个服务生成强密码
POSTGRES_PASSWORD=<生成>
REDIS_PASSWORD=<生成>
MEILI_MASTER_KEY=<生成>
SECRET_KEY=<生成>
ADMIN_PASSWORD=<生成>
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

### 3. 启动基础设施

```bash
docker compose up -d postgres redis meilisearch
```

等待健康检查通过：

```bash
docker compose ps
# 三个服务均应显示 "healthy"
```

### 4. 数据库迁移

```bash
docker compose run --rm backend alembic upgrade head
```

### 5. 启动应用

```bash
docker compose up -d
```

### 6. 验证

```bash
# 健康检查
curl http://localhost:8818/api/v1/system/health

# 期望响应：
# {"status":"ok","services":{"postgres":"up","redis":"up","meilisearch":"up"}}
```

管理后台：`http://<主机IP>:3000`。

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

### 命名模板

命名模板控制文件在 `LIBRARY_ROOT` 中的组织方式。使用 gallery-dl 的模板语法。默认模板存储在数据库（`naming_template` 表），可通过管理后台编辑。

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
