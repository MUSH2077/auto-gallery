# 多用户管理 + 手动上传 设计文档
# Multi-user Management & Manual Upload — Design Spec

> 日期: 2026-07-15 · 分支: `frontend-motion`(实施时切新分支)
> 经 brainstorming 逐题确认的决策:两个子项目都做;细粒度权限→模块开关;配置含个人偏好入库/内容过滤/上传配额;上传归属"默认个人空间 + 有策展权限可选任意创作者";权限实施选每请求查库。
> 范围外但同批执行的遗留项:R1/R2 内联组件抽出、真实浏览器性能录制(§0)。

---

## §0 遗留两项(纯执行,非本 spec 设计对象)

1. **R1/R2 组件抽出**:`TaskDetailDrawer`/`JobDetailDrawer` 从 `app/admin/jobs/page.tsx` 抽到 `src/components/`;`FullImageLightbox`/`DisclosurePanel` 从 `app/admin/works/[id]/page.tsx` 抽出。props 与行为零变化,纯移动 + import 调整,build 绿即验收。
2. **性能录制**:前置条件——用户执行 `sudo npx playwright install-deps chromium`。之后用 scratchpad 的 playwright-core 脚本扩展为 CDP tracing:录 `/admin/works`、`/admin/jobs`、`/admin` 三页,产出 long tasks 清单与 CLS 值,写入 `docs/frontend-motion-audit.md` 附录。

---

## §A 多用户管理

### A1 数据模型(alembic 迁移)

`users` 表新增列:

| 列 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `is_admin` | Boolean | false | admin 绕过全部权限检查,可管理用户;迁移时现有 bootstrap 管理员置 true |
| `is_active` | Boolean | true | false 时拒绝登录与一切请求(401) |
| `permissions` | JSONB | `[]` | 模块名字符串数组,见 A2 |
| `preferences` | JSONB | `{}` | 主题/配色/语言/外观设置(键名与前端 localStorage 现有键对齐) |
| `nsfw_visible` | Boolean | true | false 时图库/搜索强制过滤 NSFW;仅 admin 可改 |
| `upload_quota_bytes` | BigInteger, nullable | null | null=不限;供 §B 使用 |
| `upload_used_bytes` | BigInteger | 0 | 上传字节累计;删除作品时不回冲(简化,审计口径) |

### A2 模块注册表(固定,后端常量 + 前端镜像)

```
library        图库浏览:works / creators / tags / search 的读与收藏浏览入口
curation       策展操作:favorite / trash / restore / purge / dedup / merge / curation / gitllery
upload         手动上传(§B 的全部端点与页面)
subscriptions  订阅与来源:subscriptions / sources / reference(danbooru)
tasks          任务中心:jobs / import-jobs / scheduler / notifications / tasks API
system         系统与设置:admin/* 设置、data-mgmt、system、backup、logs
```

- 用户管理(users API 与 /admin/users 页)**永远仅 admin**,不在模块表内。
- 勾选模块 = 该模块内查看+操作全开(模块开关形态,无读写分级)。
- 后端单一常量 `PERMISSION_MODULES`,前端从 `/api/v1/me` 返回的注册表渲染,不各自维护。

### A3 权限实施机制(方案①:每请求查库)

- JWT 仅保留身份(username);新增 FastAPI 依赖工厂 `RequirePermission(module)`:请求时加载 user 行,顺序判定 `is_active`(否→401)→ `is_admin`(是→放行)→ `module in permissions`(否→403)。
- 现有 `RequireAdmin` 保留,仅用于用户管理与显式 admin-only 端点。
- 各 router 挂载(单独一个提交,便于回滚):works/creators/tags/search→`library`;curation/dedup/merge→`curation`;subscriptions/sources/reference→`subscriptions`;jobs/scheduler/notifications/tasks→`tasks`;admin 设置类→`system`;upload→`upload`。`/media/thumb/` 维持开放(img 标签约束),`/media/preview|original/` 沿用登录校验。
- 权限变更、禁用立即生效(下一请求即拦截)。

### A4 API

- `GET/POST /api/v1/users`、`GET/PATCH/DELETE /api/v1/users/{id}`(RequireAdmin):创建(用户名+初始密码+权限)、改权限/配额/nsfw_visible/is_active/display_name、重置密码(置 must_change_password)、删除(禁止删自己与最后一个 admin)。
- `GET /api/v1/me`:身份 + is_admin + permissions + 模块注册表 + preferences + nsfw_visible。
- `PUT /api/v1/me/preferences`:本人偏好写入(整体替换,服务端仅校验键白名单)。
- Pydantic schema 全覆盖;列表返回含 last_login_at(登录时更新)。

### A5 NSFW 内容过滤

- `nsfw_visible=false` 的用户:WorkRepository 列表/详情查询与 SearchService(Meilisearch filter)强制注入 `is_nsfw = false`;缩略图路由不单独校验(列表已不可见,直链属可接受残余风险,记录于风险节)。
- 过滤在 repository/service 层注入,不依赖前端。

### A6 前端

- `/admin/users` 列表页(仿 creators 列表:行布局、创建弹窗 Modal、启停/删除 ConfirmDialog)+ `/admin/users/[id]` 详情页(仿 creators 详情分区:账号信息、权限矩阵开关组、配额与用量、NSFW 开关、重置密码)。导航"管理"分组新增"用户"入口(仅 admin 可见)。
- 导航与路由门控:`useMe()`(TanStack Query)提供权限;AdminNav 按模块隐藏分组/链接;各页面顶层守卫无权限时渲染 403 EmptyState(不做整页跳转,保持可预测)。
- 偏好入库:theme/palette/lang/appearance 读取顺序 = 服务端 preferences → localStorage 兜底;变更时写 localStorage 并防抖 PUT /me/preferences。登录后立刻应用服务端值。
- i18n:全部新文案 zh/en 双写。

---

## §B 手动上传(依赖 §A 的用户与配额)

### B1 上传流

```
POST /api/v1/upload (multipart, RequirePermission("upload"))
  → 校验:扩展名+MIME 嗅探(jpg/png/webp/gif/mp4/webm)、单文件 ≤500MB、
    配额(超额 413)、文件名净化(uuid 重命名,原名存元数据)
  → 落盘 DOWNLOAD_ROOT/manual/{creator_dir}/{work_id}/(Path.relative_to 容器校验)
  → 合成 gallery-dl 风格元数据 JSON(title/tags/nsfw/uploaded_by/original_filename)
  → 复用 imports 队列 run_import_job:ManualProvider.parse_* 按合成元数据实现
    → Work/WorkSource(source=manual)/Asset/Tags、缩略图(pyvips/ffmpeg)、pHash、
      sha256、Meilisearch 索引 —— 与其它源完全同管线
  → upload_used_bytes 累计;返回 import_job id 供任务中心跟踪
```

### B2 归属模型

- 默认**个人空间**:首次上传自动创建 `source_creators(source=manual, source_creator_id="user:{username}")` 及对应 creator(display_name=用户显示名)。
- 拥有 `curation` 权限的用户可在上传时改选**任意现有创作者**(或新建);此时 work_source 仍为 manual,挂到所选创作者。
- 上传者记入 `work_source.raw_metadata.uploaded_by`。
- 可见性:manual 属用户主动上传行为,work_source 默认 `is_enabled=true`(在 provider 层显式声明,区别于"非 Pixiv 默认禁用"的下载源规则)。

### B3 前端

- `/admin/upload` 页(`upload` 权限门控;导航"媒体库"分组新增入口):多文件选择(拖拽)、每批共用的标题前缀/标签/NSFW 标记、创作者选择器(无 curation 权限时锁定为个人空间)、逐文件上传进度、完成后 toast + 任务中心跟踪 import job。
- 作品详情页显示"上传者"字段(source=manual 时)。

---

## 实施顺序(每步独立提交 + pytest + 部署)

```
0a R1/R2 抽组件 → 0b 性能录制(等 sudo 依赖就绪,可并行推迟)
A1 迁移+模型+UserService/Repository → A2 users API+schemas+测试
A3 前端用户页(列表+详情)+i18n → A4 RequirePermission 全路由落地+导航门控(单独提交)
A5 偏好入库+NSFW 过滤 → B1 上传后端+ManualProvider+测试 → B2 上传页+详情页上传者
```

## 测试与验收

- 后端:users CRUD/权限依赖(403/401 矩阵)/NSFW 过滤/上传管线(伪文件端到端到 import)全部 pytest;现有 350+ 用例不回归。
- 前端:typecheck+build 绿;无权限用户导航不可见对应分组、直达 URL 得 403 态。
- 部署:alembic upgrade + 全容器 recreate;NAS 侧 git pull + deploy.sh。

## 风险与已知取舍

| # | 内容 |
|---|---|
| 1 | A4 权限落地触碰所有 router——单独提交,出问题整体 revert 即回到"全员 admin 等价"状态 |
| 2 | NSFW 过滤不覆盖缩略图直链(知道 asset id 可绕过)——NAS 内网自用,接受;后续可加 /media 层校验 |
| 3 | 配额不回冲(删作品不减 used_bytes)——审计口径,admin 可手动重置 |
| 4 | worker 执行 import 时无请求上下文,上传的权限/配额判定全部在 API 层完成后才入队 |
| 5 | 偏好服务端化后,未登录页(login)仍走 localStorage 兜底 |
