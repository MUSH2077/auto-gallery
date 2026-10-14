# 系统架构

## 概述

auto-gallery 是一个分层的 Docker Compose 应用，从多个来源下载媒体，将元数据导入规范的 PostgreSQL 数据库，并通过 FastAPI 后端和 Next.js 管理界面提供服务。

## 分层图

```
┌─────────────────────────────────────────────────┐
│  管理端 (Next.js 16)                             │
│  TypeScript · Tailwind CSS · TanStack Query      │
│  端口 13000（主机）← 3000（容器）                 │
└──────────────────┬──────────────────────────────┘
                   │ HTTP REST
┌──────────────────▼──────────────────────────────┐
│  后端 (FastAPI)                                  │
│  Routes → Services → Repositories → SQLAlchemy   │
│  Providers: Pixiv · X · Iwara · Danbooru ·      │
│   Weibo · Bilibili · Pinterest · LOFTER          │
│  端口 8818（主机）← 8000（容器）                  │
│  Redis 浏览器会话 + Bearer JWT                   │
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

## 当前模块边界

- `SearchService` 仍是后端搜索对外接口。`search_language.py` 解析查询，
  `search_pagination.py` 管理稳定游标和排序，
  `search_filters.py` 编译 Meilisearch 过滤条件，
  `search_projection_fields.py` 生成确定性的索引字段。
  `search_index_definitions.py` 持有索引名称与配置，`search_projection.py`
  持有文档构建和增量投递、全量重建共用的投影定义；SearchService 保留旧调用兼容入口。
  `subscription_schedule.py` 提供纯日程计算，调度页面和搜索共用
  `scheduler_decisions.py` 的来源资格与凭据阻塞解释。
- `search_sql_filters.py` 把 SQL 作品投影的适用性与筛选规则从
  SearchService 的执行路径中分离。
- `search_work_page.py` 持有可复用的作品页资产查询语句，每次请求绑定自己的作品 ID；
  多个来源关联同一资产时只计数一次，按顺序返回的预览 ID 保持每个作品最多 10 个。
  单索引文本搜索在一次执行中复用同一个 Meilisearch 客户端。
- `api/media.py` 用一次查询取得请求资产、代表资产和最终存储状态，保留原有鉴权、
  签名 URL、ETag/304、隔离文件读取及已清除文件拒绝行为。
- `import_runner.py` 处理作品微批，`import_execution.py` 管理领取、租约与完成流程。
  `import_lifecycle.py` 按父任务、ImportJob、TaskRun 顺序锁后重读，负责 token
  仲裁、导入控制命令和父子聚合；涉及 artifact 时先获取 artifact 锁。
  `import_intent.py` 持有 artifact 分配和持久化投递准备，prepare 不提交事务，
  publish 在调用方提交之后执行；下载、上传、磁盘导入和恢复共用该实现，
  旧 worker 函数保留为持久化 RQ 记录的转发入口。
- `danbooru_import.persist_danbooru_artist` 为单项、Pixiv ID 批量和 URL 批量导入
  共用已解析作者的落库规则，按 URL 去重来源，并在调用方事务内应用可解析身份、
  别名和搜索投影意图。入口在提交前验证 attempt、提交后失效缓存，置信度分流和
  逐项结果继续由各入口负责。
- `download_lifecycle.py` 负责下载状态应用及显式的 TaskRun、receipt 和搜索投影，
  控制面、worker 失败/重试、stale 与无效派发在各自完成资格检查和取锁后共用该能力。
  worker 用领取时的 RQ ID 与 TaskRun attempt 拒绝旧执行回写，仓储不再隐式创建或
  更新 TaskRun。调用方提交后才发布持久化状态通知。`download_finalization.py`
  复用既有 bounded 子任务聚合，为导入完成、publication 关闭与恢复提供共同父级
  outcome 映射和最终化。
  `TaskEngine.apply_cancel_download/import` 只应用取消，不提交或通知，下载委派
  导入也沿用这一事务。独立控制入口提交后通知；批量清理将取消和批次项一起应用，
  最后验证 cleanup attempt，通过后一次提交再通知，失效执行的修改全部回滚。
- `operations.py` 协调管理任务发布与补偿；
  `admin_dispatch_recovery.py` 从 PostgreSQL 有界地选择到期派发意图，
  不把 Redis 队列状态当作恢复的权威依据。
- 管理操作定义关联 handler、规范化结果策略和资源 profile；
  `admin_operation_handlers.py` 执行业务，RQ worker 管理 attempt 与心跳。
  services 下的 `backup.py`、`integrity.py`、`connectivity.py` 提供独立于
  HTTP 路由的业务能力。备份和恢复候选文件公开前仍须通过持久 attempt 校验。
- HTTP 与 CLI 的收藏、标签修改共用整理命令事务，领域变更、审计和搜索 outbox
  一起提交。标签沿用 CLI 的 casefold 规则，不批量重写历史标签。永久清理先获取
  整理顺序锁再锁定作品状态，并在删除文件前重读资格；已提交恢复的作品不会因
  旧 ORM 快照被删除。
  HTTP 与 CLI 的 trash/restore 在排序锁和状态重读后共用资格、修改与审计构造，
  入口策略保留 CLI noop 审计及 HTTP 重复/缺失 ID 的处理差异；purge 保留专用文件流程。
- `gitllery/payload.py` 为 outbox 和直接投影／重建共用 segment 提交格式映射，全局
  父提交与仓库父提交分别保留，调用方仍在投影锁内读取仓库父链。
- `resource_pressure.py` 协调采样、状态和共享快照，分别委托给
  `resource_pressure_sampling.py`、`resource_pressure_state.py` 和
  `resource_pressure_snapshot.py`。
- `resource_descriptors.py` 定义子进程准入与 outbox 切片，新投递显式携带描述，
  旧 RQ 函数名仅由兼容适配器解释。管理操作的 profile 不改写 task ID/attempt
  参数。父级准入、子级租约、资源阈值与退出保护继续各自生效。
- `api/client.ts` 为 fetch 和 XHR 上传共用响应错误规范化，上传进度仍由 XHR 处理，
  CSRF 和请求会话校验保护 401 处理，网络失败与超时使用传输错误类别。
- `i18n.tsx` 提供唯一的翻译 Context 与 Provider，别名和相对路径导入均指向它。
  构建脚本读取纯词典源 `i18n-catalogs.ts`，运行时按语言加载生成的 JSON，
  继续隔离迟到的词典加载结果。
- Jobs 页用 `jobsRoute.ts`、`useJobsRouteState.ts` 管理 URL 状态，
  用 `jobTree.ts` 纯函数整理任务树，行、抽屉和批量操作通过 `useTaskCommand.ts`
  共用 `taskCommands.ts` 的动作分派、repeat-sync 意图处理和缓存失效。
  任务类型源于生成的 OpenAPI schema，只细化视图所需的字典字段。
  `operationMonitor.ts` 管理普通管理操作的恢复和轮询，`operationRefresh.ts`
  管理资源刷新，NotificationCenter 消费监控状态展示通知。页面与全局层展示同一
  操作时，共用按 TaskRun 身份建立的 `admin-operation-task` 查询；页面关闭自身
  轮询和完成回调，由全局监控负责。未交给全局的操作继续由页面监控，404/410
  会终止监控并清除活动记录。重试接受后，利用已知任务上下文和现有最小响应重新
  激活监控、隔离迟到查询并重置本次完成处理，TaskRun 身份保持不变。
  `batchImportMonitor.ts` 将批量响应适配到同一 monitor 并独占恢复，Danbooru 页面
  只消费规范化状态。历史记录保留原 storage key 和两小时恢复窗口；完成结果缺失时
  通过查询的有限重试确认，随后明确结束并提示错误。
  创作者与订阅列表通过 `useReferenceListNavigation.ts` 共用导航、会话、已加载行
  选择清理和旧分页恢复，各页继续保留筛选、数据请求和业务命令。

## 请求调度与交互列表

HTTP 队列统计、作者计数缓存、工作台 Redis 健康检查、代理和重建状态通过 `redis_budget.py` 在线程中执行同步
Redis 操作，命令截止时间为 0.4 秒，调用者等待上限为 0.6 秒；可选读取失败保留原有
降级响应，不改后台阻塞出队超时。密码计算由每个 API 进程最多两个专用线程执行，
保持 bcrypt 成本、限流、账号锁和凭证版本递增规则。
工作台的可执行文件查找也在线程中执行，避免缓慢的 PATH 查找阻塞事件循环。

虚拟列表最多持有 32 页查询，只启用当前窗口和邻近预取／恢复页；淘汰页查询可回收，
不能将缓存淘汰误判为记录删除。只有所属页的确定数据才能移除对应选中 ID。
会话独立保存已知的选中项所属页偏移，不随缓存页一起淘汰；没有归属信息的旧会话
选择继续保持待恢复状态。
订阅摘要复用一次成员批量查询完成可见性过滤及私有状态叠加。普通 Modal 与命令面板
共用焦点约束和背景隔离；素材读取失败显示重试入口并保留已有媒体，不显示成空数据。
滚动锁及背景 inert 按弹窗所有权计数，最后一个所有者退出才恢复原值，不依赖导航时
的清理顺序。

## 领域模型

所有模型使用与来源无关的命名。共享数据 vs 用户隔离数据：

```
（跨用户共享）
creator ──< source_creator
creator ──< creator_link
creator ──< subscription ──< subscription_source
work ──< work_source
work ──< work_tag
work_source ──< work_source_tag
asset ──< asset_source
tag

（用户隔离意图）
user ──< user_subscription >── subscription
user_subscription ──< user_subscription_source >── subscription_source
user ──< remote_account ──< discovery_candidate
user ──< album ──< album_work ── work

（运维 / 横切）
task_run ──< task_event        # 统一任务信封 —— 见"统一任务系统"
storage_artifact               # 下载 → 导入账本 —— 见"任务队列"
```

### 关键关系

- **user**：已有的管理员或普通用户账号，按模块授予权限。
- **creator**：规范本地身份（如"画师 A"）——跨所有用户共享
- **source_creator**：平台特定账号（如 Pixiv 用户 123456）
- **creator_link**：链接创作者到外部个人资料的 URL
- **work**：规范本地作品（可有多个 work_source）
- **work_source**：来源特定的作品记录，保留原始元数据
- **asset**：本地文件
- **asset_source**：来源特定的文件记录
- **subscription**：每位创作者唯一、跨用户共享的规范仓库
- **subscription_source**：跨用户共享的规范来源；启用/到期字段是兼容性汇总缓存
- **user_subscription**：当前用户对规范订阅的私有名称、启停与调度设置
- **user_subscription_source**：当前用户的来源启用、到期、认证健康度和可选远端账号绑定
- **remote_account** / **discovery_candidate**：账号私有的发现配置与扫描候选
- **album**：用户创建的作品收藏集（Phase 6+）
- **album_work**：album 与 work 的多对多关联

### 唯一性约束

- `(source, source_creator_id)` 在 source_creators 表
- `(source, source_work_id)` 在 work_sources 表
- `(source, source_asset_id)` 在 asset_sources 表
- `(normalized_name)` 在 tags 表
- `(creator_id)` 在 subscriptions 表
- `(subscription_id, source_url)` 在 subscription_sources 表
- `(user_id, subscription_id)` 在 user_subscriptions 表
- `(user_subscription_id, subscription_source_id)` 在 user_subscription_sources 表
- `(user_id, source)` 在 remote_accounts 表
- `(remote_account_id, source_creator_id)` 在 discovery_candidates 表
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
- **远端发现队列**：`worker-discovery` 独占并监督 `discovery` 队列。scheduler
  只准入到期远端账号；provider 分页与游标 checkpoint 在子 worker 中执行。任务只携带不透明的
  task/account ID，绝不携带凭据。
- **持久化 RQ 导入**：下载产物记录在 PostgreSQL 中，有上限的 RQ 导入消费者通过执行令牌认领任务，并通过作品身份租约认领工作，实现避免重复占有的可重启恢复。消费者按批次共用入库资源名额，不在整个任务期间持有同一把锁。
- **任务超时**：所有入队调用使用 `job_timeout=7200`（2 小时），防止 RQ 杀掉长时间运行的下载任务（默认 180s）。
- **代理预检**：启动 gallery-dl 前，worker 先做 DNS 解析 + 代理连通性检查（非阻塞诊断）。
- **状态机**（权威来源：`backend/app/models/task_state.py`）：`enqueued → downloading → downloaded → importing → complete`，非终态可转入 `paused`、`cancelled`、`failed`、`stale`。旧状态串（`pending` → `enqueued`）通过向后兼容映射接受。存在 `downloaded → complete` 短路：当下载未产生新元数据（内容已在归档中）时直接完成，避免卡在等待导入。暂停任务跳过执行；`cancelled` 为终态但保留数据库记录以便审计。僵死检测使用 Redis 心跳键（`task:{job_id}:heartbeat_ts`，90s TTL）。超时/失败时支持部分导入恢复。`task_state.py` 定义合法转换，`TaskEngine` 协调控制请求，导入状态通过 `import_lifecycle.py` 的锁后仲裁与聚合入口更新。

## 统一任务系统 (`task_runs`)

下载、导入、管理操作（备份、重建索引、磁盘导入）都通过一个面向用户的任务信封呈现，使管理端**任务**页（`/admin/jobs`）拥有统一的队列/进度/历史视图。

- **`task_run`**（`models/task_run.py`）：每个可追踪工作单元一行。`kind`（`download` / `import` / `admin`）加 `subject_type` + `subject_id` 以松耦合方式引用权威领域行（`download_job`、`import_job` 等）——无外键，因此任务可以先于或晚于其领域记录存在。携带展示字段（`title`、`source`、`source_url`）、进度快照（`progress_current`/`progress_total`/`progress_data`）、`priority`、`attempts`，以及用于僵死检测的 `last_heartbeat_at`。`parent_task_id` 用于归组批量/子任务。
- **`task_event`**：某次运行状态转换的追加式审计轨迹；随父记录级联删除（`ON DELETE CASCADE`）。
- **`TaskService`**（`services/tasks.py`）：写入路径——创建运行、记录事件、规范化状态，并把下载/导入领域行合并进统一列表。
- **`TaskEngine`**（`services/task_engine.py`）：控制请求入口（暂停 / 恢复 / 取消 / 重试 / 优先级 / 按筛选批处理），导入命令委托给生命周期模块，并通过 Redis pub/sub 发布变更以驱动 WebSocket 实时更新。
- **权威载荷仍在领域表**：`task_run` 是信封；`download_jobs` / `import_jobs` 仍是各自字段的真实数据源。清空任务（数据管理）会同时删除 `task_runs` **和**领域任务表。

## 数据流

私有意图保存在 `user_subscriptions`、`user_subscription_sources`、
`remote_accounts` 与 `discovery_candidates`。规范的 `subscription`/
`subscription_source`、creator、work、asset 与文件继续全局共享去重。规范来源的汇总启用/
到期字段只是从私有成员需求派生的兼容缓存。

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

下载执行把文件写入 PostgreSQL `storage_artifacts` 账本，`import_intent.py`
创建并发布持久 RQ 导入意图；`worker-import` 认领当前 attempt，在租约下分组处理。
Provider 元数据映射为作者、作品来源、资源和标签，账本状态记录进度以支持恢复。
媒体派生请求持久化后交给后台执行，导入完成不要求同步生成全部缩略图。
原始文件保留在 DOWNLOAD_ROOT，元数据和派生文件放在 LIBRARY_ROOT；成功导入后清理 JSON。

作者刷新用一次 LEFT JOIN 收集订阅／仓库，用 UNION 合并身份与规范化 URL 两条作品关联路径；无仓库订阅仍包含在依赖范围内。同一次刷新请求的 subscription、creator、repository membership 条件合并查询，使用 UUID 数组参数和 500 条 keyset 分页，这三种范围内同一动作的重叠成员只登记一次。独立刷新请求仍递增 outbox 版本，删除请求仍最后生效，删除作者前收集的依赖快照继续用于刷新存续作品与删除关联文档。

批量导入在写入作品和资源的同一事务内保存媒体处理、元数据整理和搜索索引的
outbox 记录，并按任务类别合并唤醒意图；只有外层事务成功提交后才发布唤醒。
事务或保存点回滚不会发布已回滚的意图。通知失败时，已提交的 outbox 记录保留，
由恢复路径继续处理。因此，“导入完成”不等于“缩略图和搜索已经可用”，
各项后台结果需要分别确认。

Danbooru 参考批次是独立管理操作，单项、Pixiv ID 和 URL 入口共用
`persist_danbooru_artist`，每项在提交前验证当前管理操作 attempt；
它们不消费 Redis Stream，也不绕过媒体导入租约。

### Danbooru 参考流程
```
管理员触发 Danbooru 画师导入
  → 拉取 Danbooru 画师页面
  → 解析画师标签、关联 URL
  → 匹配可解析身份、artist ID 与活动来源链接
  → 归属冲突明确失败，否则应用共同落库规则
  → 校验当前操作 attempt，提交后失效缓存
```

## 可靠性与恢复

由于 gallery-dl 直接写盘，且下载 → 导入交接跨越两个队列，系统提供若干恢复路径让数据库与磁盘实际内容保持一致。它们全部幂等，绝不删除或重新下载。

- **磁盘导入**（`services/disk_import.py`，`POST /api/v1/admin/library/import-from-disk`）：扫描 `DOWNLOAD_ROOT/{source}/`，把磁盘上存在但从未入库（账本状态 ≠ `done`）的文件以一个合成 `recovery` download_job 导入。`services/disk_identity.py` 会先从本地元数据补建真实的 `creator → subscription → subscription_source → source_creator` 链（Danbooru 富化为尽力而为，并带纯元数据兜底），使恢复的作品挂到正确创作者。运行在 `operations` 队列；Redis 锁（`library:disk-import:active`）防止并发运行。
- **导入管线恢复**（`services/import_recovery.py`）：对安全缺口的保守式自动修复——例如为已有待处理元数据的 `downloaded` 任务重新入队导入。只读健康状态由 `services/pipeline_health.py` 报告。
- **创作者自动重链**（`services/creator_reconcile.py`）：每次订阅同步扫描时，按身份匹配把孤立的 `source_creators`（`creator_id IS NULL`）链接到其订阅的创作者。它沿用 `import_runner` 的自动链接规则，因此身份模糊的收藏/转发源绝不会被错链。
- **Worker 启动孤儿对账**：启动时把卡在 `downloading`/`importing` 的任务标记为 `stale`，并把磁盘上无对应任务的目录重新拾取导入。

磁盘导入管理任务完成表示扫描与子任务发布结束；其发布数量不是作品数量，实际结果需检查所有导入子任务及对应下载任务。全局重放与限定来源重放使用相同的导入分类：未变化的已有作品属于正常幂等结果，不能因新增数量为零而判定全部失败。新增或更新作品、已有作品及异常跳过的计数分别保留，终态进度按三者之和表示已处理工作组。少量异常低于配置的跳过率阈值时完成并提示跳过；全部失败或达到阈值时仍判失败。完成结果分类的批次，无论完成或失败，均保存统计及总工作组数，跳过项的台账错误继续保留；多批次汇总沿用各子任务状态并保留上述计数。

平台下载和磁盘导入共用图片扩展名定义，支持 AVIF 并登记为 `image`，资产 MIME 为 `image/avif`。原图保留，预览生成 WebP；静态 AVIF 使用 SHA-256 和版本化 pHash 指纹。多帧 AVIF 预览首帧，仅使用完整文件 SHA-256 做精确去重，不使用首帧 pHash。缩略图优先走 libvips；其无法打开 AVIF 时，Pillow 回退读取首帧。应用内 Pillow AVIF 解码固定为单线程，沿用媒体任务的资源准入和原子缩略图写入。历史遗漏文件可通过磁盘导入补登记，已有作品走追加资产／幂等更新路径。

视觉指纹使用纯 NumPy real FFT 实现的 DCT-II，对 Pillow LANCZOS 生成的 32×32 灰度样本计算。不保留 SciPy 临界回退，旧 pHash 不保证逐 bit 相同；版本边界明确，生产库需要之后重新计算。对于无法使用等比工作图的超长非交错 8 位 RGB/RGBA 单帧 PNG，保留顺序灰度采样路径以限制内存。其他 PNG 布局及图片格式沿用原路径，Pillow 的解压炸弹检查继续执行。未提供共享工作图时，宽度超过 400 像素的缩略图使用 libvips 文件入口，使 JPEG 可在解码时缩小；尺寸、方向、透明度及原子写入策略保持。SSIM 在原图上就地处理 EXIF 方向，已为 RGB 时复用像素，保留 256×256 缩放、评分公式和判断阈值。

### 索引与资产去重收尾

搜索文档投递取得远端 taskUid 后，先将任务身份写入持久资源保护记录，
并提交 pending 状态的数据库记录，再进行最多 250 毫秒的短暂确认。
未完成时每隔 50 毫秒读取一次状态，同时保留原有 3 秒的数据库确认与保护记录
清理预算；HTTP 查询不持有数据库事务。设置任务完成只推进到文档阶段，
不会提前确认源版本，也不会在同一次资源准入中继续提交下一次写入。

短确认窗口结束、状态查询失败或剩余完成预算不足时，已知任务仍为 pending，
首个后继任务等待 1 秒。后续任务未完成时按 2／4／8／15 秒退避，
普通延迟轮询中的网络错误仍等待 15 秒。持久任务记录、按提交版本确认、
远端资源保护及提交结果未知时的恢复规则继续保留。远端索引完成、
本地确认和下一批开始仍是不同时间点。

延迟确认让慢任务释放本地 worker，并控制查询频率；短确认用有限的本地等待
和额外状态查询，换取快速任务更早收尾。这属于调度策略的性能优化。

资产候选先通过 SHA／视觉条件，再读取作者、发布时间等元数据；被视觉条件否决的候选不会执行这些补分查询。通过条件的左右资产在一次来源关联查询中读取，按资产 ID 分组后沿用原评分。候选来源筛选及加锁后的范围复核继续独立执行，代表选择、作品与来源关联，以及硬链接／隔离前的原图核验保持原规则。未来到期的隔离清理不属于当前导入批次的即时收尾。

去重 outbox 入口按实际路径加载依赖：扫描管理能力在扫描入口加载，整理能力在应用分组时加载，案例媒体链接与整理提交的响应 Schema 在相应接口路径加载。RQ 函数路径、整理审计和响应结构继续沿用现有定义。实际 RQ 父进程在模型注册后、首次工作子进程 fork 前配置 ORM 映射；子进程独立建立数据库连接，启动准备与稳定驻留成本单独记录。

资源感知 worker 使用 `ResourceAwareScheduler` 推广到期任务。调度器沿用 RQ 的推广与锁规则，在推广成功后通过已有队列工作通道发送一次唤醒提示；提示失败仍可通过 worker 超时检查恢复，队列记录保持权威。首次确认与远端任务退避、资源准入和收尾恢复规则继续保留。

### 后台任务的资源恢复

“没有待处理任务”和“任务因资源不足暂时不能执行”是不同结果。媒体和资产去重
执行器会报告资源等待，协调器据此登记等待标志，不把持久任务标记为完成。
PostgreSQL outbox 记录仍是任务状态的权威来源，Redis 中的唤醒归属和等待标志
仅用于帮助恢复。

worker 释放资源后，父进程检查此前等待的任务；监听 scheduled 队列的监管进程
还会在 worker 空闲时消费 `resource:control` 通知，并在启动和重连后检查已有
等待任务。媒体选择、恢复检查和实际执行使用一致的内存要求与资源判断；
恢复不会绕过内存保护、资源锁、延迟执行时间或尚未结束的远端搜索资源占用。

恢复检查和任务发布有执行时限，超时保留等待任务；通知失败不会把已经完成的
业务任务改成失败。原有约 60 秒一次的扫描继续保留，用于通知或 Redis 等待标志
丢失后的恢复。这些机制减少的是资源恢复后的无谓等待，资源仍不足时任务继续
等待。操作排查见[运行手册](RUNBOOK.md#background-outbox-recovery)。

## Gitllery —— 磁盘上的策展历史

PostgreSQL 的策展提交和变更始终是权威来源。策展写入在同一事务中保存持久投影意图，
`gitllery_outbox.py` 规划目标仓库并以受限切片异步投影；磁盘失败保留可重试意图，
不撤销已提交的领域修改。outbox 租约、attempt 校验与投影锁保护重放和写入顺序。

当前 Gitllery v1 格式在各仓库 `.gitllery` 中保存 `manifest.json`、`config.json`
及压缩、内容寻址的 `segments/`。先写 segment，再原子推进 manifest。
全局提交父链与仓库父链分别表达，增量投影和构建共用载荷映射；
旧对象／索引格式保留兼容读取，不再代表当前写入协议。

`services/gitllery/builds.py` 管理重建、验证及发布，`services/gitllery/rebuild.py`
读取历史用于重建。`/api/v1/curation/gitllery/` 下的维护命令使用既有管理操作与资源准入，
管理端面板展示状态并发起命令；API 不同步写入所有仓库，也不把磁盘当作在线数据库的替代品。

## 认证

管理端使用存于 Redis 的可撤销随机浏览器会话。会话 Cookie 仅当前主机可用，
设置 HttpOnly、SameSite=Lax 和 / 路径；HTTPS 来源额外设置 Secure。前端只能读取
与会话绑定的 CSRF 值。浏览器写请求必须携带该值和精确允许的 Origin；WebSocket
使用一次性会话票据并校验握手来源。登出撤销会话，改密轮换会话；Redis 故障时返回
服务不可用，不接受失效会话。

脚本/API 客户端继续使用 `POST /api/v1/auth/login` 返回的 Bearer JWT。
显式 Bearer 优先，Bearer 无效时不回退 Cookie。两种入口沿用停用用户、强制改密和
模块权限规则。`ACCESS_TOKEN_EXPIRE_MINUTES` 同时决定两种登录的时长：应用默认
10080 分钟，Compose 默认 1440 分钟。迁移后浏览器用户需要重新登录一次。

Bearer 凭证携带不可复用的用户主键 `uid` 和凭证版本 `cv`；用户名仅用于操作者标签，
不再作为授权查找键。管理员重置密码和两个换密入口都在密码事务内递增版本，认证读取
当前活跃及强制换密状态。部署时先执行增列迁移 `0159bc20de46`，原用户名式 Bearer
凭证需要重新登录。WebSocket 票据和事件转发同样绑定主键／版本，已建立连接不能继承
同名新账号的权限。取消管理员、禁用及删除在共同事务 advisory lock 内刷新记录、
检查并修改最后一个活跃管理员约束。

局域网 HTTP 入口仍可用，但该连接上的 Cookie 传输没有 HTTPS 保护；需要保护传输
时应使用 HTTPS 反向代理入口。

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
| `GALLERYDL_CONFIG_ROOT` | `/gallerydl-config` | 持久 gallery-dl 配置/cookie 与不含密钥的任务配置 |
| `APP_CONFIG_ROOT` | `/app-config` | 应用运行时配置 |
| `PERSONAL_AUTH_TMP_ROOT` | `/run/auto-gallery-secrets` | 仅 `worker-download` 使用的 tmpfs，存放权限 0600 的个人认证覆盖文件；永不备份 |

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
│   │   └── jobs/                       # 不含密钥的任务配置；备份时排除
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

个人账号认证覆盖文件不属于 NAS 持久目录。下载 worker 将它们写入专用非持久
tmpfs `/run/auto-gallery-secrets`，并在使用前验证该边界。

### 存储规则

- **downloads/**：原图仓库。原始图片/视频文件按 `{source}/{creator_name}/{source_work_id}/` 组织并长期保存。导入时从 gallery-dl 的扁平输出移动至此。JSON 元数据文件处理后被删除。路径中不包含 job_id 层级。
- **library/**：索引层。仅保存每个作品的元数据与派生图片。与 downloads 使用相同的 `{source}/{creator_name}/{source_work_id}/` 结构，通过 source_work_id 链接，不存放原始媒体。
- **gallery-dl 输出**：导入前的 worker 短生命周期输出。导入过程中文件被重组到长期保存的原图仓库中，JSON 文件被删除。
- **缩略图与海报**：图片通过 pyvips 生成 400px WebP 缩略图；MP4/WebM 由 ffprobe 探测，并生成 400px WebP 缩略图与最长边不超过 1280px 的海报。派生文件位于 LIBRARY_ROOT，原始媒体不会被修改。
- **预览/原图**：直接从 DOWNLOAD_ROOT 通过 `/media/preview/{asset_id}` 和 `/media/original/{asset_id}` 提供。预览需要短期签名 URL；原图接受管理员 Bearer 鉴权或短期签名 URL。
- **视频播放**：授权后的单资产票据提供两小时 `/media/stream/{asset_id}` 访问权限，以 HTTP Range 方式传输原始 MP4/WebM；不执行转码、HLS、纯音频或 ugoira ZIP 播放。
- **metadata.json**：导入时为每个作品写入。包含 work_id、source、source_work_id、title、posted_at、creator、assets 数组。
- **source_work_id**：平台特定的作品 ID（如 Pixiv 作品 ID "38362603"）。在 downloads/ 和 library/ 中都用作目录名。链接两个存储树。

## 文件、上传与回滚边界

Cookie 配置使用 `services/cookie_paths.py` 解释路径。相对路径以
`GALLERYDL_CONFIG_ROOT` 为根，目录内绝对路径、历史 `/gallerydl-config/...`
容器路径和 `/cookies/...` 简写继续支持。解析符号链接后仍须位于允许目录内；
配置读取、写入和有效下载配置对越界返回可诊断错误，不退回无配置下载。

手动上传每次最多 20 个文件、单文件最多 500 MiB、文件累计最多 1 GiB，
另预留 2 MiB multipart 开销。网页通过独立的 `/api/upload` API 入口关闭请求体解析，直接流式转发到后端，
避开 Next.js 外部 rewrites 的请求体克隆与默认 10 MiB 截断；前端原有的 `/api/v1/upload`
也会内部转发到该入口，其他 API 继续使用原有 rewrites。
转发保留认证、Origin、CSRF、后端状态及 Retry-After，并在客户端断开时取消上游请求。
文件、批次、请求总量和账号配额超限分别返回可区分的 413 错误码；网页只在配额不足时
停止后续文件，合并上传还会预先检查 20 文件与 1 GiB 上限。`UploadRoute` 在 multipart 解析前计算实际接收字节，
不依赖 Content-Length；复制时分别执行单文件、批次和剩余配额预算。
最终用量由带配额条件的数据库 UPDATE 原子计费，与任务、产物在同一事务提交，
并发上传与 purge 释放不使用旧对象读改写。复制失败清理自身临时文件；移动或保存失败
回滚用量并删除本次作品目录，已提交文件／任务则保留计费，由持久导入意图处理后续执行。

未指定目标创作者的个人上传，在创作者、订阅、manual 来源及别名完整且一致时，
复用数据库中的身份关联。每次上传仍检查这些记录，并用一次批量 upsert 更新别名
观测时间，不在请求之间缓存身份状态。关联不完整或已变化，以及明确指定创作者的
上传，继续走通用身份创建与修复路径。该优化减少重复的身份维护查询，用户容量
仍由原有原子 UPDATE 记账，事务提交继续由调用方负责。

前台上传从复制、持久化到导入发布均持有现有 POSIX 维护屏障的共享锁，注册清空任务
由 worker 父侧持有独占锁直至执行结束；维护期间上传返回带 Retry-After 的
`503 / maintenance_in_progress`。媒体清空保留 `.locks`，文件删除成功前保留所有权
和计费事实，随后通过共同生命周期取消待执行任务，只返还本次删除上传的计费。
文件失败返回 `status=partial` 和逐路径诊断，注册任务标为失败，数据库和用量保留供
重试；部分文件可能已删除。修复文件系统问题后重试同一清空即可。若后续数据库提交
失败，已经删除的文件不能回滚，但保留的数据库事实仍可用于重试清空。

HTTP purge 从资产资格查询、用量释放到文件删除、提交和结果读取同样持有共享维护锁，
阻止清空与 purge 交错返还同一笔用量；维护期间 purge 也返回上述 503 和重试提示。

离线恢复和部署回滚必须先停止 writer，并通过 Compose 确认没有运行、重启中或暂停的
容器，才能切换文件、数据库、Redis 数据或应用镜像。停止或检查失败时，补偿和前台启动
都被阻断；离线 journal 记录 blocked 组件，部署回执记录 `paused`。
管理员解决停止失败后重跑原回滚命令，保留原有 schema-forward 策略。

## 环境变量

应用使用的关键环境变量：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `BACKEND_PORT` | `8818` | 后端 API 主机端口（映射容器 8000） |
| `MEDIA_PLAYBACK_TTL_SECONDS` | `7200` | 单资产 MP4/WebM 播放票据的有效期（秒） |
| `ADMIN_WEB_PORT` | `13000` | 管理端主机端口（映射容器 3000） |
| `CORS_ORIGINS` | `http://localhost:13000` | 跨来源 API 客户端允许来源 |
| `BROWSER_SESSION_ORIGINS` | `http://localhost:13000` | 会话、CSRF 和 WebSocket 的精确 HTTP/HTTPS 浏览器允许来源 |
| `BACKEND_INTERNAL_URL` | `http://backend:8000` | admin-web SSR 访问后端的内部 URL |
| `NEXT_PUBLIC_WS_URL` | 当前站点 `/api/v1/ws` | 浏览器连接实时任务 WebSocket 的公开地址 |
| `DATABASE_URL` | （必需） | PostgreSQL 连接字符串 |
| `REDIS_URL` | （必需） | Redis 连接字符串 |
| `SECRET_KEY` | （必需） | Bearer JWT 签名密钥 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Compose：`1440` | Bearer 令牌和浏览器会话有效时长（分钟） |
| `TIMEZONE` | `UTC` | 调度器和显示的时区 |
| `DOWNLOAD_ROOT` | `/downloads` | 原始文件存储路径 |
| `LIBRARY_ROOT` | `/library` | 元数据 + 缩略图存储路径 |
| `GALLERYDL_CONFIG_ROOT` | `/gallerydl-config` | gallery-dl 配置路径 |
| `APP_CONFIG_ROOT` | `/app-config` | 应用配置路径 |
| `PERSONAL_AUTH_TMP_ROOT` | `/run/auto-gallery-secrets` | 下载 worker 的个人认证覆盖 tmpfs |

## API 路由分组

所有路由均以 `/api/v1` 为前缀，除非另有说明。
本节用于说明架构边界，并非完整端点清单。由 CI 对照运行时校验的
[OpenAPI 与 AsyncAPI 契约](api/README.zh.md)才是规范的机器可读接口。

### `/api/v1/system`
健康检查、队列统计、存储统计、清除失败任务、日志缓冲。

端点：
- `GET /health` 与 `GET /ready` — 公开存活检查与依赖就绪检查
- `GET /storage` — 图库逻辑存储统计
- `GET /workbench` — 仪表盘状态与最近活动
- `GET /queue-stats` — 等待/运行/失败任务数量
- `GET /scheduler-decisions` — 当前仓库调度决策
- `POST /clear-failed-jobs` — 清除所有失败的下载/导入任务
- `GET /logs` — 查看缓冲日志输出

### `/api/v1/admin`
设置、gallery-dl 配置、系统信息、完整性、备份、导入进度、代理测试、调度器控制、去重、合并候选、重建索引、数据清理。

端点包括：
- `GET /settings` / `PUT /settings` — 系统设置 CRUD
- `POST /reset-settings` — 重置设置为默认值
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
统一复合搜索：图库实体由 Meilisearch 执行，运维集合由 SQL 执行。

端点包括：
- `GET /` — 分组搜索作品、创作者、标签、仓库、订阅、任务或调度决策
- `POST /assist` — 服务端解析、诊断、建议与查询组合

语法详见[复合搜索语言](search.md)。全量索引重建由数据管理 API 排队执行。

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
- `GET /thumb/{asset_id}` — LIBRARY_ROOT 中的私有 400px WebP 缩略图；有效会话／Bearer 或绑定资产和用途的短期签名必须在读取及派生请求前通过验证
- `GET /preview/{asset_id}` — 来自 DOWNLOAD_ROOT 的原始文件，需要短期签名 URL
- `GET /poster/{asset_id}` — 派生的大尺寸 WebP 视频海报，需要签名 URL
- `GET /stream/{asset_id}` — 支持 HTTP Range 的原始 MP4/WebM，需要单资产播放票据
- `GET /original/{asset_id}` — 来自 DOWNLOAD_ROOT 的原始文件（下载用）


### 有上限的下载执行

下载消费者与执行名额使用同一套并发计算。页面的 1～5 设置仍在重启后生效，
部署上限与设备上限共同限制消费者数；显式设备下载上限只影响下载，其他队列和
调度预算继续由自动档位控制。下载子进程继承 supervisor 的实际消费者数，
避免多个消费者仍共用一个执行名额。

Redis 原子核对每个下载与全部资源任务的总内存预留，短暂的文件准入锁和固定
名额锁在协调键丢失、缩小配置时仍限制执行数。旧单网络键存在时继续与新名额
互斥，但升级前须停止旧版下载进程。默认单名额沿用原 profile 内存预留，并行
模式每个下载至少预留 256 MiB。同平台任务继续用 Redis 与本地文件锁保护共用
归档；归档身份查询失败时在领取和启动下载之前延期。准入过程中收到取消，先
等待 Redis 申请结束并清理本任务占用，再传播取消；下载进程组清理仍先于释放
来源锁与名额。部署和连接预算见[部署配置](deployment-profiles.md#download-worker-concurrency--下载并发)。

### 有上限的导入消费者

`IMPORT_WORKER_CONCURRENCY` 设置 1～4 个导入消费者，默认 1；直接传入
`worker_entrypoint.py imports N` 时命令行数量优先，修改环境变量后重新创建
导入服务。独立维护消费者在 supervisor 的按队列统计中单独计数。

执行领取保留 DownloadJob → ImportJob → TaskRun 锁顺序和当前执行令牌。
作品领取使用代表元数据行、`FOR UPDATE SKIP LOCKED` 与来源/作品身份租约。
每次领取和入库阶段仍取得共用入库资源预留及文件锁，等待时不持有 SQL 事务。
多个消费者可以重叠准备数据和等待，但数据库批次仍由资源准入串行控制。
每批事务共同提交作品、资产及媒体、元数据整理、搜索 outbox；恢复和旧令牌
失效保护不依赖消费者数量。连接预算见
[导入部署配置](deployment-profiles.md#import-worker-concurrency--导入并发)。

### 图片并行处理

`MEDIA_WORKER_CONCURRENCY` 同时设置图片执行名额和 Compose 的 operations
消费者数量，范围为 1～8，默认 1。普通图片和超大图共用这一池。
Redis 在同一次原子操作中检查名额与总内存预留，POSIX 共享屏障和独立名额锁
在 Redis 协调键丢失时仍限制本机执行数量。同一资产的数据库领取与版本检查
继续阻止重复计算和过期结果写回。

并行模式每张图片至少预留 768 MiB；内存压力或其他资源任务可能使实际执行数
低于配置上限。视频、索引、去重、维护及尚未结束的远端搜索写入继续使用互斥
保护。媒体唤醒任务各自持有有限的名额；缩小配置时保留仍在运行的所有者，
并停止超出新上限的后继任务。图片算法版本、源文件校验和结果租约检查沿用
原有规则。

API 和所有 Python worker 使用相同配置。数据库连接与内部库线程预算见
[部署配置](deployment-profiles.md#image-worker-concurrency--图片处理并行数)。
按设备的 CPU、可用内存和实际负载选择数量，不将一个配置值作为所有设备的默认。

### 共享视觉工作图

媒体派生保留至少 1600 万像素、需要更新 pHash 且解码布局适合的静态图片的共享工作图路径，
建立长边不超过 1024 像素的工作图，并在缩放后的像素上完成 EXIF 方向校正。
工作图在一次媒体任务内复用，用于 pHash、256×256 RGB 分析样本，以及尺寸足够时尚未生成的卡片缩略图；
进程级 libvips 缓存上限仍为 64 MiB。已完成的首卡缩略图不重复生成，长条图保持原有缩略图尺寸策略。长宽比超过 4:1 及其他不支持的静态图片布局继续直接采样。

只有在同一媒体任务缺少卡片或尺寸时，原宽超过 400 像素、面积不超过 200 万像素且不含 PNG 色键透明度的单帧 RGB/RGBA PNG
才会在该任务内共享全尺寸 Pillow 图。JPEG、已有首卡和其他布局继续沿用原路径；该小图工作图不会预先生成 SSIM 样本。
已经是 RGB 的 `WorkingImage` 采样不再复制一份 RGB 像素；直接采样路径会在加载数值库前释放解码后的原图。
其他图片的分析样本仍在 SSIM 需要时按原有直接缩放方式生成。

pHash 输入版本为 `numpy-phash-oriented-adaptive-v3`，媒体派生版本为 `media-visual-v3`。
已有指纹只有版本和源文件指纹都匹配时才能复用；旧 pHash 必须重新计算，新旧版本不交叉比较。
SHA256 精确去重、代表图保留规则和按 EXIF 校正后的显示宽高比候选检查保持不变，数据库原始尺寸也保持不变。

分析样本继续保存在 library 下的 `.visual-samples`，采用 1024 个固定槽位；
每份样本为 256×256 RGB 无损字节及版本、资产 ID、源大小/修改时间、
已有 SHA256 和显示尺寸。全部槽位约 193 MiB，冲突、文件缺失或指纹失配
都按缓存未命中重建，不保存 1024 工作图。写入采用临时文件加原子替换。
一次去重任务的样本内存缓存限制为 16 MiB，不长期保留浮点数组。
SSIM 仍使用原有 256×256 样本、公式、阈值和缓存策略。资产删除清理其拥有的槽位。
文件隔离前仍重新读取原图核对 SHA256，并以新鲜原图重新生成样本复核 SSIM，
分析缓存不能代替该安全检查。

升级已有图库时先停止 scheduler 和各 worker，避免混合版本期间执行视觉
分组或文件隔离，再使用新代码、媒体资源配额的临时 worker-import 容器运行：

```sh
docker compose run --rm --no-deps worker-import python -m app.services.visual_fingerprint_rebuild --enqueue --process
```

该入口通过媒体 outbox 分批登记、逐个按资源许可处理；中断后可继续运行。
无参数运行仅查看状态。`remaining_available` 和 `pending_fingerprints` 都必须为 0，且处理无失败，才恢复消费者，
随后从已有管理入口执行一次全局去重扫描。已经清除或隔离、原路径不可用的
历史成员计入 `unavailable_historical`，保留旧指纹和分组记录。它们不作为
可重新分析的原图；可用代表图参与升级。旧证据保留，新证据版本为
`numpy-phash-oriented-adaptive-v3` 和 `media-visual-v3`。本升级不会自动删除或重建已有分组。
