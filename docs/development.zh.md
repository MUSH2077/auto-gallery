# 开发指南

请先阅读[中文贡献指南](../CONTRIBUTING.zh.md)，了解贡献流程、必要检查、隐私规则和
PR 要求。

## 仓库结构

```text
auto-gallery/
  backend/
    app/
      api/            FastAPI 路由
      models/         SQLAlchemy 模型
      repositories/   数据库访问
      schemas/        API 契约
      services/       业务逻辑与编排
      providers/      来源 URL 与元数据适配
      jobs/           后台任务入口
    alembic/          数据库迁移
    tests/
  admin-web/
    src/
      app/admin/      App Router 页面和布局
      components/     共享 UI 原语与领域组件
      lib/api/        类型化 API 客户端
      lib/            认证、国际化、导航、主题与偏好
    tests/e2e/        Playwright 浏览器测试
  docs/
  scripts/
  docker-compose.yaml
```

当前 Docker Compose 服务：

- `postgres`、`redis`
- `backend`
- `worker-download`、`worker-import`、`worker-operations`、`worker-discovery`
- `scheduler`
- `admin-web`

API 和 Python worker 共用 backend 镜像，只有下载 worker 执行 gallery-dl。

`MEDIA_WORKER_CONCURRENCY` 同时设置 operations 消费者数量和图片执行名额，
范围 1～8，默认 1。API 和所有 worker 使用相同值，修改后重新创建应用容器。
普通图和超大图共用这些名额；视频、索引、去重、维护、远端索引保护及导入优先级
继续沿用原资源规则。并行模式每张图片至少预留 768 MiB，可用内存可能使实际
执行数低于配置上限。该参数不设置上传、下载或导入 worker 数量。
比较不同数量时保持 operations 总数据库连接预算相同，并限制
VIPS／OpenBLAS／OMP／MKL 内部线程，避免进程与线程同时扩容。

按设备的 CPU、可用内存和实际负载选择数量，默认值不是硬件建议。
`test_parallel_media_slots.py`、`test_parallel_media_wakes.py` 和
`test_parallel_media_rq.py` 分别覆盖资源准入、唤醒任务所有权与真实 RQ 执行。
这些测试使用独立、可丢弃的 PostgreSQL 和 Redis 环境。

## 后端

### API worker 并发

Compose 的 backend 使用 `backend/api_entrypoint.py`，由 Uvicorn 原生 supervisor
启动 API 进程。`API_WORKER_CONCURRENCY` 默认 1，允许 1～4；非法值会在启动时失败。
应用导入前，launcher 按进程数向下分配单个 backend 容器的 API 总预算：数据库稳定连接 8 加溢出连接 4，
每进程为 `1 = 8 + 4`、`2 = 4 + 2` 或 `4 = 2 + 1`；总
`MEILI_SEARCH_CONCURRENCY=4` 同样按进程分配，但每进程至少保留 1。直接用多进程
`uvicorn` 会绕过这套预算分配，不属于正式部署命令。

`API_THREAD_POOL_SIZE` 默认为 backend 容器总计 16 个阻塞线程名额，应用导入前向下分配为 1 = 16、2 = 8、4 = 4。该上限分别应用于 asyncio 默认线程池和 AnyIO 线程 limiter；两者是独立线程池，16 不是两者合计的线程上限。API1 虽然跳过 Redis 租约，仍使用 16 个线程名额。

每个进程都可以处理前台 HTTP 和 WebSocket。Redis 租约选出一个维护任务所有者，并在
所有权变化时保护资源快照的写入；其他进程读取共享健康和资源快照。前台指标低频汇总，普通请求
路径不执行 Redis 操作。除非已经按目标设备 CPU、内存和连接预算做出选择，否则保留默认 1。
增加 API 进程数不会自动增加容器的 CPU 额度；两者应一起选择，并按目标负载验证。

### 下载并发

下载启动、子进程准入和健康信息共用 `services/download_concurrency.py`。
原数据库设置仍按重启规则生效，由部署和设备上限约束；可选的
`DOWNLOAD_DEVICE_CONCURRENCY_LIMIT` 只覆盖下载。内部运行值
`DOWNLOAD_EXECUTION_CONCURRENCY` 由 supervisor 传递，不单独设置。
健康信息区分目标与运行中的消费者数，资源记录另行统计活动网络预留。

在独立 PostgreSQL 和 Redis 中运行 `test_download_concurrency_config.py`、
`test_worker_concurrency.py`、`test_parallel_download_slots.py` 和
`test_download_archive_identity.py`，覆盖默认值、配置上限、归档互斥、内存准入、
协调键丢失、缩容、取消和进程退出。现有下载生命周期与交接测试继续验证进程
终止和可靠导入发布。部署预算见[部署配置](deployment-profiles.md#download-worker-concurrency--下载并发)。

### 导入并发

`IMPORT_WORKER_CONCURRENCY` 设置 1～4 个导入消费者，默认 1。未明确传入
`imports N` 时使用环境变量；supervisor 按队列报告目标数量和存活数量。
维护消费者独立，重启和调度所有权仍按队列保留。该参数调整消费者数量，
不放开共用的入库批次资源名额。

在独立 PostgreSQL 和 Redis 中运行 `test_import_concurrency_config.py`、
`test_worker_concurrency.py` 和 `test_parallel_import_execution.py`，使用
独立进程与数据库会话验证重复领取、相同作品身份和旧执行令牌失效后的保护。
保留既有生命周期、最终提交恢复、资源名额和提交后唤醒测试。比较时统一每进程
连接池及容器资源限制，分别记录任务完成与图片、搜索全部就绪。
部署预算见[导入并发配置](deployment-profiles.md#import-worker-concurrency--导入并发)。

### 搜索投递确认

搜索写入取得远端任务 UID 后，先持久保存任务身份和 pending 数据库记录，再检查
完成状态。延迟轮询让慢任务释放本地 worker；短暂的提前确认用有限等待及额外
状态查询，换取快速任务更早完成。新任务的确认窗口最多 250 毫秒，状态读取间隔
50 毫秒，同时保留原有 3 秒用于数据库确认和持久保护记录清理。快速任务可以在
同一次投递中完成；慢任务或确认查询失败时保留已知 UID，首个后继等待 1 秒，
后续未完成轮询按 2／4／8／15 秒退避。HTTP 查询不持有数据库事务。

设置任务完成只推进到文档阶段，不提前确认源版本，也不在同一次资源准入中
开始另一写入。提交结果未知时继续保留保护，确认仍匹配已提交的 outbox 版本。
`test_search_delivery.py` 和 `test_search_delivery_recovery.py` 覆盖快速完成、
待处理任务、读取失败、取消以及不重复提交的恢复。

### 技术栈

Python 3.12、FastAPI、SQLAlchemy 2.0 async、Alembic、PostgreSQL 16、
Redis/RQ、PostgreSQL 搜索和 gallery-dl。

### 本地迭代

先启动数据服务，再从虚拟环境运行 API：

```bash
docker compose up -d postgres redis

cd backend
python -m venv venv
source venv/bin/activate
python -m pip install --require-hashes -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Compose 映射的后端地址为 `http://localhost:8818`；本地 Uvicorn 默认使用
`http://localhost:8000`。

### 测试与检查

容器路径最容易复现：

```bash
docker compose run --rm -T --volume "$PWD/backend:/app" \
  -e PYTHONDONTWRITEBYTECODE=1 backend \
  python -m pytest
docker compose run --rm -T --volume "$PWD/backend:/app" \
  -e PYTHONDONTWRITEBYTECODE=1 backend \
  ruff check app tests
```

单文件测试可在 `pytest` 后追加路径。本地虚拟环境使用相同的 Python 命令，但不需要
前面的 `docker compose run ... backend`。

### 依赖

`backend/requirements.txt` 由 `requirements.in` 通过 pip-tools 生成。
gallery-dl 等 Provider 敏感依赖必须固定版本。

```bash
cd backend
python -m pip install pip-tools
pip-compile --allow-unsafe --generate-hashes \
  --no-emit-index-url --no-strip-extras \
  --output-file requirements.txt requirements.in
```

升级依赖后运行 Provider 测试和 Docker Compose smoke 路径。
Dependabot 会将 minor 与 patch 更新分组到同一个 PR，但每个 major 更新保持独立 PR，
以便单独审查迁移工作与兼容性风险。

### 数据库迁移

```bash
docker compose run --rm backend \
  alembic revision --autogenerate -m "description"
docker compose run --rm backend alembic upgrade head
docker compose run --rm backend alembic downgrade -1
```

必须审查自动生成内容。列重命名经常被识别为破坏性的删除和新增。PR 中需要说明回填、
重建索引、长时间锁表和数据丢失风险，并验证 upgrade 与 downgrade。

### 模块边界

- 路由负责认证、校验和委托。
- Service 负责业务规则与编排。
- Repository 负责数据库访问。
- Provider 负责 URL 校验、规范化和来源元数据解析，不直接访问数据库。
- 导入与维护操作必须幂等。
- 后台状态转换必须遵守既有任务状态机。

## 管理前端

### 技术栈与结构

前端使用 Next.js 16.3.6、React 19.2.8、TypeScript、Tailwind CSS、TanStack Query 和
Playwright。主要产品模块位于 `src/app/admin`；共享页面容器、导航、反馈、对象列表、
文本溢出和交互原语位于 `src/components`。

API 访问拆分在 `src/lib/api/client.ts`、`types.ts`、`types.generated.ts` 和
`index.ts`。先将后端 OpenAPI 契约导出至 `docs/api/openapi.json`，再从该文件生成前端类型：

```bash
cd admin-web
npm run generate:api-types
```

### 本地运行与检查

```bash
cd admin-web
npm ci
npm run dev
```

提交 PR 前：

```bash
npm run typecheck
npm run build
npm run test:e2e
```

环境中没有 Playwright 浏览器时：

```bash
npx playwright install chromium
```

### 前端要求

- 所有面向用户的文字同时提供中英文。
- 页面层级复用共享导航元数据，不在各页面重复定义。
- 检查窄屏和长文本，不依赖固定像素宽度。
- 保留键盘焦点、触屏目标、语义名称和降低动效支持。
- 把加载、空、错误、部分成功和无权限状态作为一等界面。
- 当主要工作流与公开图片明显不同时，更新脱敏文档素材。

## 仓库检查

CI 的 Node 回归组运行 `tests/*.test.mjs`、`tests/*-contract.mjs`、
`tests/*.contract.mjs` 及明确列出的 Jobs 和任务动作测试。
`helpers.mjs`、`typescript-loader.mjs` 是辅助模块；`danbooru-import-flow`、
`test-client-nav`、`test-processing-nav`、`test-result-restore` 是手动浏览器工具，
不作为 Node test runner 输入。新增行为回归放入这些组或既有后端／Playwright 分片，
不另建冻结覆盖清单。

Cookie 路径应通过配置读取、保存和有效下载配置构造验证，越界和符号链接使用无敏感内容
的临时文件。上传同时验证真实请求流、multipart 解析和并发 PostgreSQL 保存：
最多 20 个文件、单文件 500 MiB、批次 1 GiB，并预留 2 MiB multipart 开销。
前置预算不能替代最终原子计费；注入读取失败、取消、移动和提交失败后，核对临时文件及用量补偿。

账户回归通过正式 HTTP 登录和真实 PostgreSQL 事务验证，覆盖重置密码、两个换密入口、
禁用和用户名复用后的旧 Bearer，以及票据和事件转发；受控交错的管理员移除必须保留
至少一个活跃管理员。上传／清空回归使用真实 POSIX 锁和 Redis 辅助租约，覆盖复制、
未完成导入、已导入作品、文件权限失败及重试。失败清空保留计费与所有权事实，取消通知
只在成功提交后发布。用例接入既有账户、上传、操作和 worker CI 分片。

请求调度回归使用隔离 Redis 的暂停、拒绝连接和无响应场景，确认其他请求和事件循环
仍可推进；登录使用真实 bcrypt，另验证最多两个计算线程。摘要在 1／10／50 个 ID
及未拥有、重复、inactive 成员场景下保留一次成员查询。浏览器检查浏览 10／50／100 页
后的修改请求量、选择恢复、双向 Tab、字段标签和素材错误／重试／空数据。
工作台须覆盖完整刷新路径，在健康检查边界注入暂停，避免前面的队列读取消耗故障。
选择恢复须覆盖选中页被淘汰后刷新、加载完保留页再回到原行，以及所属页确认删除
和旧会话没有归属信息的场景。
弹窗回归还须覆盖作者表单上叠加命令面板并导航到任务页，确认原 overflow／inert 恢复、
侧栏与顶部控件可用，以及鼠标滚轮实际能够滚动。
命令面板还须验证 Tab／Shift+Tab 和方向键后的焦点、选中项及 Enter 目标一致，覆盖
清空按钮、组合输入事件、实际搜索建议、仅查询参数导航、权限过滤和重新打开。

`scripts/verify-runtime.sh` 从活跃管理员记录签发当前身份／版本凭证，优先使用已换密账号。
首次部署仅有待换密管理员时，验证文档和业务接口返回 403，并提示完整文档访问验证尚待完成；
通过正常登录换密后重新运行该脚本。没有活跃管理员则失败，脚本不修改账号安全状态。

缩略图 GET 和条件 GET 必须在读取及派生请求前验证会话／Bearer 或绑定资产和用途的票据。
同源浏览器图片会携带会话 Cookie，需保留 private 缓存和 ETag 行为，另测匿名和已撤销会话。

回滚测试使用命令替身，不接触在线部署。停止失败、超时或 writer 仍活跃时，
切换和前台启动必须阻断。查看 `journal.json` 或 `rollback-receipt.env`，
解决停止问题后重跑原回滚命令。主机测试的命令替身应使用与测试一致的 Python 版本，
将测试虚拟环境的 `bin` 放在 PATH 前面。

```bash
docker compose --env-file .env.ci config --quiet
bash scripts/privacy-scan.sh
bash scripts/package-release.sh ci
```

CI 还会运行完整后端、前端、仓库和 Compose smoke 检查。详见
[.github/workflows/ci.yml](../.github/workflows/ci.yml)。
