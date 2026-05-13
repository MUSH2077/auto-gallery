# 客户端 API 约束

## 规则

后端必须同时服务管理后台和未来的远程客户端（Flutter/移动端）。所有 API 设计必须考虑多用户场景、移动带宽限制和客户端友好的响应格式。

## 用户模型（客户端前必须实现）

系统必须支持多用户账号：

```
user
  - id: UUID
  - username: String（唯一）
  - email: String（唯一，v1 可选）
  - hashed_password: String
  - role: Enum(admin, user)
  - is_active: Boolean
  - created_at, updated_at
```

**权限级别**：
- `admin`：完全访问所有数据、系统设置、作业管理、所有创作者/作品
- `user`：访问自己的订阅、自己的相册、公开作品；不能管理系统设置或为其他用户触发下载

**数据隔离规则**：
- `subscription`：按用户隔离（添加 `user_id` 外键；移除 `unique(creator_id)` 约束——多个用户可订阅同一创作者）
- `album`：按用户隔离（添加 `user_id` 外键）
- `download_job`：通过 subscription → user 继承隔离
- `creator`、`work`、`asset`、`tag`：**跨用户共享**（不按用户重复创建创作者记录；作品下载一次，所有引用之）

## 认证（客户端就绪）

必须同时支持管理后台和远程客户端：

- **基于 JWT**（客户端路由不用简单 API key）
- 登录端点：`POST /api/v1/auth/login` → 返回 `access_token` + `refresh_token`
- Token 刷新：`POST /api/v1/auth/refresh` → 新的 `access_token`
- Access token：短期（15 分钟），Refresh token：长期（30 天）
- Admin-web 路由（`/api/v1/admin/*`）要求 `role=admin`
- 客户端/用户路由要求已认证用户（任意角色）
- 公开路由（如有）：健康检查、媒体服务（媒体可能需要 token）

**过渡方案**：
- Phase 1-5：简单 API key 仅用于 admin 路由（尚无用户模型）
- Phase 6+：添加用户模型、JWT 认证，将 admin 认证迁移至带 admin 角色的 JWT
- 不要实现两套并行的认证系统；用户模型到来时用 JWT 替换 API key

## 客户端 API 路由分组

除 admin 路由外，添加面向客户端的路由分组：

```
/api/v1/auth           登录、刷新、登出、当前用户
/api/v1/me/
  /subscriptions       我的订阅
  /albums              我的相册/收藏集
  /feed                已订阅创作者的最新作品
/api/v1/creators       浏览、查看创作者详情（公开）
/api/v1/works          浏览作品（公开）、查看作品详情
/api/v1/search         搜索可访问的作品
/api/v1/albums         自有相册 CRUD（按用户隔离）
/media                 媒体服务（可能需要认证 token）
```

Admin-only 路由保持在 `/api/v1/admin/*`。

## 相册 / 收藏模型

用户需要将作品整理到相册：

```
album
  - id: UUID
  - user_id: FK → user
  - title: String
  - description: Text（可空）
  - is_public: Boolean（默认 false）
  - cover_asset_id: FK → asset（可空）
  - sort_order: Integer
  - created_at, updated_at

album_work
  - album_id: FK → album
  - work_id: FK → work
  - sort_order: Integer
  - added_at: DateTime
  - unique(album_id, work_id)
```

相册按用户隔离。用户不能修改其他用户的相册。管理员可查看所有相册。

## 移动端友好的响应设计

客户端 API 响应必须针对移动端优化：

**缩略图**：
- 每个 asset 响应必须包含多尺寸缩略图 URL
- 导入时生成缩略图：`thumb_sm`（200px）、`thumb_md`（600px）、`thumb_lg`（1200px）
- 缩略图 URL 模式：`/media/thumb/<size>/<asset_id>.jpg`
- 客户端按需获取适当尺寸；网格视图绝不用全图

**分页**：
- 默认每页：20 项（移动端）、50 项（管理后台）
- 最大每页：100 项
- Feed 和时间线使用游标分页（新数据到达时稳定）
- 固定列表（相册、订阅）使用偏移分页

**响应封装**：
```json
{
  "data": [...],
  "cursor": "eyJsYXN0X2lkIjogIi4uLiJ9",
  "has_more": true,
  "total": 1523
}
```

## 客户端 ↔ 服务器交互模式

- 客户端轮询获取新数据（REST）；v1 无 WebSocket
- 订阅同步状态：轮询 `GET /api/v1/me/subscriptions/:id` 获取 `last_synced_at`
- 下载进度：轮询 `GET /api/v1/download-jobs/:id` 获取状态和进度
- 客户端绝不直接触发下载；通过 `POST /api/v1/me/subscriptions/:id/sync` 请求订阅同步（创建下载任务，可能需要管理员批准）

## 离线考量（v2+）

v1 不要求。但 API 设计不应阻碍：
- 基于时间戳的增量同步（`?updated_since=2026-05-01T00:00:00Z`）
- 媒体端点的 ETag / If-None-Match 支持
- 以上延后实现，但架构不得阻塞
