# 风险登记册

## 高风险

### 1. 多用户数据模型过渡

**风险**：当前 Phase 1-5 设计没有用户模型。在 Phase 6 添加需要：
- Schema 迁移：向 `subscription` 表添加 `user_id`
- API 破坏性变更：所有订阅端点从全局变为按用户隔离
- 认证迁移：从 API key 到 JWT
- 数据迁移：现有订阅必须分配给默认管理员用户

**影响**：如果不提前规划，Phase 6 将变成重大重构而非增量添加。已有 API 消费者（admin-web）将损坏。

**缓解措施**：
- Phase 1-5 设计时意识到 `user_id` 将被添加到 subscription
- 在 Phase 6 前将订阅端点保持在 admin 路由后（避免过早公开 API）
- 使用 repository 模式使数据访问层变更局部化
- 预留 `user_id` 列名；不要用于其他用途
- Phase 6 迁移方案：添加可空 `user_id` -> 用默认 admin 回填 -> 设为非空
- Admin-web 基于 `/api/v1/admin/*` 路由构建（不因用户模型添加而改变）

**决策**：Phase 1-5 保持 admin-only。Phase 6 添加用户模型+迁移路径。不提前引入用户模型。

---

### 2. gallery-dl 版本 / 输出格式稳定性

**风险**：gallery-dl 频繁更新。锁定版本会导致源站点变化时提取器失效。不锁定版本则输出格式变化可能使元数据解析器静默失败。

**影响**：下载中断、导入失败、元数据缺失

**缓解措施**：
- 在 `requirements.txt` 中锁定已知可用的 gallery-dl 版本
- 添加集成冒烟测试：用 gallery-dl 下载已知 URL，验证 JSON 输出结构与解析器期望一致
- 升级 gallery-dl 前先运行冒烟测试套件
- 版本化 gallery-dl 配置 schema

**决策**：锁定版本 + 冒烟测试。禁止自动升级。

---

### 3. 跨来源创作者身份映射

**风险**：跨平台关联账号（"Pixiv 用户 123456" <-> "@artist_handle on X"）是根本性难题。个人资料 URL 嵌入在自由文本字段中。许多创作者在不同平台使用完全不同的名字。自动化准确率有限。

**影响**：管理员手动映射负担重。创作者记录碎片化。

**缓解措施**：
- 接受自动化映射最高只能达到约 60% 准确率
- 以手动映射为主要路径
- Danbooru 参考充其量是建议引擎，而非权威来源
- 添加 `creator_link.confidence` 字段（0.0-1.0）用于建议链接评分
- 建议链接的管理审核队列
- 绝不让自动映射成为 Phase 1-6 的前置依赖

**决策**：手动优先 + 建议引擎。架构可容纳此方案。

---

### 4. DOWNLOAD_ROOT -> LIBRARY_ROOT 原子性

**风险**：gallery-dl 写入 `DOWNLOAD_ROOT/<job_id>/`。导入过程将文件移动到 `LIBRARY_ROOT/`。若 worker 在执行过程中崩溃：
- DOWNLOAD_ROOT 中有文件但无导入任务（孤儿下载）
- DB 记录指向移动失败的文件（损坏引用）

**影响**：存储浪费、媒体 URL 损坏、数据库不一致

**缓解措施**：
- 任务状态机：`pending -> downloading -> downloaded -> importing -> complete`
- Worker 启动时扫描 DOWNLOAD_ROOT 中的孤儿目录，与 `download_job` 表对账
- 导入必须完全幂等：用相同文件重新运行不产生重复
- 利用 `(source, source_asset_id)` 唯一性约束防止重复 asset 记录
- 同一文件系统内（同一 Docker 卷）的文件移动使用 `os.rename` 保证原子性

**决策**：状态机 + 启动对账 + 幂等导入。

---

## 中等风险

### 5. Meilisearch 索引一致性

**风险**：向 PostgreSQL 和 Meilisearch 双写可能漂移。定期重建索引意味着搜索结果可能滞后。

**缓解措施**：
- v1：管理员触发的全量重建索引（手动但一致）
- v2：应用层双写 + 定期对账
- 接受 v1 中搜索结果可能滞后数分钟

---

### 6. Provider 抽象泄漏

**风险**：Pixiv 元数据（系列、标题结构）与 Iwara（声优、3D 标签）、X（话题标签、线程上下文）、Danbooru（标签分类）和微博（话题标签模式）差异显著。强制通过相同 `parse_*` 方法可能导致尴尬的最小公分母 schema。

**缓解措施**：
- `raw_metadata` JSONB 列是逃生阀——一切来源特有数据存于此
- 类型化字段仅捕获可清晰映射的内容
- admin-web 可有 provider 特有组件，按来源渲染 `raw_metadata`
- Provider `parse_*` 方法尽量提取类型化数据；不丢弃任何信息

---

### 7. 单一 worker 瓶颈

**风险**：一个 worker 一次处理一个任务。同步拥有 1000+ 作品的创作者需要数小时/数天。

**决策**：v1 接受此风险（个人归档，非实时服务）。
Worker 可水平扩展：`docker compose up --scale worker=3`。只要每个任务使用唯一的 DOWNLOAD_ROOT 子目录（`<job_id>`），扩展即安全。调度器可错开订阅同步时间避免惊群效应。

---

### 8. 多用户存储共享

**风险**：多个用户订阅同一创作者时，如果数据不共享将重复下载相同文件。Creator/Work/Asset 必须是全局的，而非按用户分割。但这带来隐私考量：一个用户的订阅向所有用户暴露了该创作者。

**缓解措施**：
- `creator`、`work`、`asset`、`tag` 表是全局的（共享，无 user_id）
- `subscription`、`album`、`download_job` 表按用户隔离（有 user_id）
- 下载一次，通过 `asset_sources` 多次引用
- 创作者列表对所有已认证用户可见（对个人/小群体归档可接受）
- 若以后需要私有创作者：添加 `creator.visibility` 字段

**决策**：共享 creator/work/asset 数据模型。仅订阅和相册按用户隔离。

---

### 9. 远程客户端带宽与图片加载

**风险**：Flutter 客户端通过移动数据加载全分辨率图片将很慢且消耗大量带宽。

**缓解措施**：
- 导入时生成缩略图：200px、600px、1200px 宽度
- 客户端 API 同时返回缩略图 URL 和全分辨率 URL
- 客户端网格/列表视图用缩略图，详情视图才用全图
- 媒体路径添加 `?width=` 查询参数用于动态调整大小（未来，pyvips 可实现）
- 媒体响应头 Cache-Control：不可变资源使用长 max-age

**决策**：导入时生成多尺寸缩略图。v1 不做动态调整大小。

---

### 10. 客户端认证 token 生命周期

**风险**：JWT access token 每 15 分钟过期。Flutter 客户端必须透明刷新。如果刷新逻辑有 bug，用户看到认证错误。如果 refresh token 过期（30 天），用户必须重新登录。不在局域网时远程重新登录在没有 VPN 的情况下不可能。

**缓解措施**：
- Flutter HTTP 拦截器（dio）处理 401 -> 刷新 -> 重试，对用户透明
- Refresh token 存储在平台安全存储中
- 刷新失败时：清除 token，重定向到登录页面
- Refresh token 到期：到期前 7 天显示通知
- 管理员可为受信任客户端发放长期 access token（未来）

**决策**：标准 JWT access+refresh 模式。dio 拦截器实现透明刷新。

---

### 11. NAS HDD I/O 性能

**风险**：Gallery-dl 下载和图像处理（缩略图生成、哈希计算）均为 I/O 密集型操作。在机械硬盘 NAS 上，大型创作者的首次同步可能使磁盘饱和。

**缓解措施**：
- 缩略图在导入时生成，而非首次请求时
- 接受大型创作者的首次同步会很慢
- 使用 pyvips 高效生成缩略图（比 Pillow 更快、内存占用更低）
- Worker 在后台运行；UI 不会因慢速 I/O 而阻塞

**决策**：接受首次同步延迟。使用 pyvips 提高效率。

---

## 已解决

以下条目为已通过实施处理的风险。

### 风险 A：X/Twitter API 可行性（显式禁用）

**原始担忧**：X 已大幅限制 API 访问。gallery-dl 的 X 提取器可能永久失效或需要付费 API。

**当前状态**：X/Twitter 显式禁用（`can_download=False`，`supports_gallerydl=False`）。Provider 作为占位符存在，提供 URL 验证和标签解析，但下载管线不会为 X 来源创建任务。UserTweets GraphQL 端点和 SearchTimeline 回退均已知不可靠。待 Pixiv 管线稳定后重新评估。Dockerfile 仍保留 SearchTimeline 回退补丁，以便日后重新启用。

---

### 风险 B：队列后端选择（已确认）

**原始担忧**：RQ vs Celery 此前未定。RQ 更简单但故障恢复功能较少。

**解决方案**：选定 RQ 并在生产环境中使用。复用已有 Redis、配置更简单。任务模型（`download_job`/`import_job`）抽象了队列。所有 enqueue 调用使用 `job_timeout=7200`。如有需要仍可迁移至 Celery。

---

### 风险 C：gallery-dl 认证/cookie 过期（已解决）

**原始担忧**：Pixiv 会话 cookie 过期导致下载静默失败。

**解决方案**：已实施：
- `subscription_source` 上的 `auth_healthy` 布尔值按来源追踪
- `subscription_source` 上的 `last_successful_auth` 时间戳
- 认证状态页面位于**设置 -> Auth Status**，按来源显示健康状况
- 连通性测试端点 `POST /admin/gallerydl-config/test-connection` 在**设置 -> gallery-dl Config**中按来源提供
- 僵死任务检测：卡在"downloading"超过 2 倍超时的任务标记为 stale
- 健康端点检测近期发生认证失败的来源
- admin-web 按订阅来源显示认证健康指示器

---

### 风险 D：Iwara gallery-dl 支持（已解决）

**原始担忧**：gallery-dl 是否支持 Iwara 未知。若不支持，Iwara 需保持占位状态。

**解决方案**：Iwara 现已为完全可用的下载 provider。`can_download=True`，`supports_gallerydl=True`，`supports_tags=True`。支持视频和图片下载，可配置画质偏好（Source、1080、720 等）。同时支持用户名/密码和 cookie 认证。

---

### 风险 E：管理员认证机制（已解决）

**原始担忧**：Phase 6 前需确定管理员认证方案。

**解决方案**：JWT access + refresh token 模式已完整实现。后端：`auth.py` 中的 `create_access_token`/`decode_access_token`，`auth_api.py` 中的 login/refresh/me 端点，admin 路由要求 Bearer JWT。Admin-web：`auth.tsx` 中的 `AuthProvider` 上下文包裹整个应用，透明 token 刷新，localStorage 持久化，用户名/密码登录页面。

---

### 风险 F：调度容差窗口与重启守卫（已解决）

**原始担忧**：容器重启时，调度器可能在意识到已有已排队任务前重新扫描所有订阅，导致重复的同步任务。

**解决方案**：
- `seed_sync.py` 在入队前检查 "scheduled" RQ 队列中是否已存在 `sync_subscriptions` 任务。仅在无已有任务时才引导初始化。
- 调度器使用 `+-scan_interval/2` 容差窗口：仅当当前时间落在某计划时间槽的半个扫描间隔内时，才同步订阅。
- `_should_sync_now()` 函数检查上次同步是否发生在计划时间槽之前，防止在同一窗口内重复触发已完成的同步。
- 僵死任务检测（2 倍超时）防止僵尸任务阻塞后续同步。
- 追赶逻辑：如果上次同步超过 24 小时前，则无论时间窗口如何都触发一次性追赶同步。

---

### 风险 G：时区感知调度（已解决）

**原始担忧**：调度器需要遵循 NAS 服务器的本地时区以支持固定时间计划（如"每天凌晨 2 点同步"）。

**解决方案**：
- `subscription_defaults`（system_settings 表）中的 `TIMEZONE` 配置，默认 `"UTC"`。
- 按部署覆盖：管理员可在**设置 -> Subscription Defaults**中更改时区。
- 基于 `ZoneInfo` 的时区解析，无效时区名时回退到 UTC。
- 所有计划时间比较（`scheduled_times`、`last_synced_at`）均以配置的时区评估。
- 支持所有 IANA 时区标识符（如 `"Asia/Shanghai"`、`"America/New_York"`）。

---

### 风险 H：Dockerfile 安全补丁（已解决）

**原始担忧**：gallery-dl 的 X/Twitter 提取器包含已知会返回 401 错误的 SearchTimeline 回退逻辑，可能掩盖真实认证问题。

**解决方案**：Dockerfile 应用 sed 补丁从 gallery-dl 的 twitter 提取器模块中移除 SearchTimeline 回退。仅使用 UserTweets GraphQL 端点。

---

## 低风险

### L1. Alembic 迁移执行
通过 `docker compose run --rm backend alembic upgrade head` 在启动前运行迁移。添加启动检查：若有待执行迁移则拒绝启动。

### L2. 文件命名 / 重新组织
gallery-dl 在下载时命名文件。导入任务可重新组织。Asset 路径在导入后存储于数据库，非导入前。若命名模板在下载和导入之间变更，无路径损坏风险。

### L3. Danbooru 阶段排序
创作者身份（Phase 4）无需 Danbooru（Phase 5）即可工作。Danbooru 丰富身份信息，并非使其可用。排序正确。

### L4. gallery-dl 归档数据库
gallery-dl 的 SQLite 归档文件追踪已下载的 URL。调度器定期对 DOWNLOAD_ROOT 中的 archive-*.sqlite3 文件执行 VACUUM。如果归档文件增大，vacuum 开销可能与下载 I/O 冲突。

### L5. Provider 数量增长
随着 provider 数量增长（目前 8 个：Pixiv、X、Iwara、Danbooru、Pinterest、LOFTER、Bilibili、微博），admin-web 设置页面和 gallery-dl 配置必须相应扩展。每个新 provider 在 gallery-dl 配置 UI 中添加一个选项卡，在 API 中添加字段。这在可管理范围内，但增加了文档和维护负担。

---

## 方案变更汇总

| 变更 | 触发风险 | 缓解产物 |
|---|---|---|
| RQ 选定为队列后端 | #B（原 #5） | CLAUDE.md -- 技术栈 |
| `creator_link.confidence` 字段 | #3（原 #2） | CLAUDE.md -- 风险衍生决策 |
| 下载任务状态机 | #4 | CLAUDE.md -- 风险衍生决策 |
| Worker 启动孤儿对账 | #4 | subscription_sync.py -- 僵死检测 |
| `last_successful_auth` 在 subscription_source | #C（原 #7） | subscription_source 模型 |
| `auth_healthy` 在 subscription_source | #C（原 #7） | subscription_source 模型 |
| v1 管理员触发 Meilisearch 重建索引 | #5（原 #6） | CLAUDE.md -- 风险衍生决策 |
| gallery-dl 输出格式冒烟测试 | #2（原 #1） | 测试套件 |
| X provider 显式禁用（占位符） | #A（原 #3） | x.py provider -- can_download=False |
| pyvips 优先于 Pillow | #11 | CLAUDE.md -- 风险衍生决策 |
| JWT 认证 access+refresh token | #E（原 #14） | auth.py、auth_api.py、auth.tsx |
| Iwara 完全启用（原为占位） | #D（原 #13） | iwara.py provider -- can_download=True |
| 按来源连通性测试 | #C（原 #7） | admin.py POST /gallerydl-config/test-connection |
| admin-web 认证状态页面 | #C（原 #7） | Settings -> Auth Status |
| 调度器容差窗口 + 重启去重 | #F（新增） | subscription_sync.py、seed_sync.py |
| 时区感知调度 | #G（新增） | subscription_sync.py、ZoneInfo、TIMEZONE 配置 |
| Dockerfile 移除 SearchTimeline 回退 | #H（新增） | Dockerfile |
