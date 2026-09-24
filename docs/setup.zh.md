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

### 局域网 API 参考

管理员登录后，可在 admin-web 主机的 `/api/docs` 打开可交互的 Swagger
UI，或在 `/api/redoc` 查看只读参考。两个界面及其静态资源都由本机提供。
版本化 HTTP 与 WebSocket 契约详见[局域网 API 契约](api/README.zh.md)。

Swagger 中的 **Authorize** 必须填写 `POST /api/v1/auth/login` 返回的显式
JWT Bearer；浏览器会话 Cookie 只用于打开文档本身。独立的局域网客户端调用
API 时，必须把它的协议、主机和端口完整加入逗号分隔的 `CORS_ORIGINS`
白名单。携带凭据时不要使用 `*`。

任务页实时进度使用 WebSocket。默认会连接当前站点的 `/api/v1/ws`；如果你的 NAS 反向代理没有转发 WebSocket upgrade，或你直接通过 `admin-web` 端口访问页面，请在 `.env` 中设置公开地址后重新构建 admin-web：

```bash
# 直连 backend 端口
NEXT_PUBLIC_WS_URL=ws://192.0.2.10:8818/api/v1/ws

# HTTPS 反代
NEXT_PUBLIC_WS_URL=wss://autogallery.example.com/api/v1/ws
```

如果 WebSocket 不可用，任务页会自动降级为轮询。每次浏览器 WebSocket
连接都通过普通 API 申请 30 秒一次性票据，握手必须带允许的 `Origin`；
撤销会话后，该会话的票据不能再建立连接。

管理界面会提供 AGPL 网络使用所需的对应源代码入口。官方构建默认指向上游仓库；
Fork 或修改版部署必须把这个构建时变量设置为实际运行版本的源代码地址：

```bash
NEXT_PUBLIC_SOURCE_CODE_URL=https://github.com/your-name/your-fork
```

修改任何 `NEXT_PUBLIC_*` 值后都需要重新构建 `admin-web`。

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

### 远端账号凭据密钥

远端关注发现需要独立的 32 字节 URL-safe base64 密钥，禁止复用 `SECRET_KEY`：

```bash
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

把输出写入 `.env` 的 `REMOTE_CREDENTIAL_KEY`。使用与数据库凭据相同的加密运维密钥
备份流程保存它，并与数据库/媒体备份分开管理。密钥丢失会使所有已存远端凭据无法解密；
错误密钥、错误的用户/来源/账号 AAD 或被篡改的密文都会导致认证解密失败。当前不支持在线
轮换：更换前必须安排停机，并原子迁移全部凭据；禁止混合密钥部署。

配置 X OAuth 时，provider 回调地址必须注册为公开的管理端页面，而不是后端 API，
并把同一个绝对 URL 写入 `.env`：

```bash
X_OAUTH_CLIENT_ID=<公开客户端 ID>
X_OAUTH_REDIRECT_URI=https://autogallery.example.com/admin/discovery
```

管理端文档 `<head>` 中的脚本会在 hydration 前从浏览器历史同步删除 `code` 与
`state`，再把它们仅一次放入
`POST /api/v1/remote-accounts/x/oauth/callback` 的 JSON body；后端刻意不提供 GET
callback 契约。管理端应用会抑制 `/admin/discovery` 的入站请求日志，但边缘反向代理
会在 JavaScript 清理 URL 之前先收到原始请求。必须让反向代理对此路径省略或脱敏 query；
例如 Nginx access log 可对 `/admin/discovery`（或全部路径）记录 `$uri`，不要记录
`$request_uri`。禁止记录 OAuth callback 的请求 header 或 body。

除 AES-GCM 密文外，明文凭据不得进入其他 PostgreSQL 字段、Redis、`TaskRun`、manifest、
API 响应或日志。下载 worker 只在权限 `0700` 的 `PERSONAL_AUTH_TMP_ROOT` 中生成权限
`0600` 的认证覆盖文件。Compose 仅在 `worker-download` 内把该路径挂载为专用 `tmpfs`；
worker 启动及每次私有任务前会清理崩溃残留，正常和错误退出均删除本任务文件。若该路径可持久化、
不可用、经过符号链接或与备份根目录重叠，worker 会拒绝启动。作为纵深防御，备份估算与归档
还会排除 `gallerydl-config/jobs`。删除远端账号会立即清除密文和未导入候选；已导入成员关系与
共享作品继续保留。

`.env.example` 中所有 discovery rollout 开关默认 `false`。按“私有成员 → Pixiv 预览 →
Pixiv 自动导入 → X 预览 → X 自动导入 → Bilibili 预览 → Bilibili 自动导入”顺序启用。
自动开关只有在对应预览开关与私有成员底座同时开启时才有效。关闭开关会停止新的发现执行，
但不会删除账号、候选、成员、订阅或媒体。

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

此命令启动全部服务：postgres、redis、meilisearch、一次性 `migrate` 服务、backend、
worker-download、worker-import、worker-operations、scheduler 和 admin-web。backend 与
各 Worker 会等待 `migrate` 成功完成，不再在每次重启时重复执行迁移。

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

修改会自动保存到 `config.json`。首次启动时，auto-gallery 会自动创建此文件，并补齐缺失的文件组织默认值，不覆盖已有自定义规则。建议通过管理后台配置 gallery-dl 提取器；手动编辑 `config.json` 亦可作为备选方式。

### 文件组织

文件组织在 **设置 > gallery-dl 配置 > 文件组织** 中配置，并保存到 `GALLERYDL_CONFIG_ROOT/config.json`。可使用 gallery-dl 的模板语法，例如 `pixiv/{user[account]}/{id}`。如果 NAS 上的文件组织不符合预期，请检查 `data/config/gallery-dl/config.json` 中对应 `extractor.<source>.directory` 和 `extractor.<source>.filename`。

## 备份与恢复

系统内建备份与恢复功能，可通过管理后台 **设置 > 备份与恢复** 访问。备份内容包括：

- PostgreSQL 数据库（创作者、订阅、作品、标签、设置、任务历史）
- gallery-dl 配置（提取器设置、Cookie、认证令牌）
- 应用配置
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
