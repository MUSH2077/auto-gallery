# Deployment Profile · 部署配置

The base `docker-compose.yaml` is a portable project-local profile. PostgreSQL,
Redis, Meilisearch, and the one-shot migration service retain hard memory, CPU,
PID, and no-swap fuses. Application services retain PID/OOM/log protections but
have no hard memory, swap, or CPU quotas; adaptive host-reserve governance,
bounded queue concurrency, and per-process budgets keep background work
cooperative.

基础 `docker-compose.yaml` 是可移植的项目级配置。PostgreSQL、Redis、
Meilisearch 和一次性迁移服务保留内存、CPU、PID 及禁用 Swap 的硬保险丝。
应用服务保留 PID/OOM/日志保护，但没有硬内存、Swap 或 CPU 配额；自适应宿主机
储备、受限队列并发与进程内部预算使后台工作保持协作。

Ordinary local deployment builds a source-digest candidate. Formal releases use
`scripts/deploy.sh --verified <manifest>` and never rebuild accepted images.
Host pressure is recorded, not used as a device-specific deployment veto.

Database migrations run in the one-shot `migrate` service. Backend and workers
wait for it to complete, while Meilisearch is deliberately not a backend startup
dependency: an unavailable search index must degrade search, not take down the
whole application.

## Browser sessions · 浏览器会话

Set `BROWSER_SESSION_ORIGINS` to the exact browser origins used at this
deployment, for example `http://192.0.2.10:13000` and the HTTPS reverse
proxy origin. Include the scheme and port, separated by commas. This list
governs browser login, credentialed reads, CSRF-protected writes and WebSocket
handshakes; it is separate from `CORS_ORIGINS` for API clients. Browser
sessions use the existing Redis service and fail closed when it is
unavailable. Changing from legacy browser tokens requires users to sign in
again. HTTPS gives the session cookie the Secure attribute. The supported
LAN HTTP entry transmits cookies without HTTPS protection; limit that entry
to a trusted network or use the HTTPS reverse proxy.

将 `BROWSER_SESSION_ORIGINS` 设置为当前部署实际使用的精确浏览器来源，
例如 `http://192.0.2.10:13000` 以及 HTTPS 反向代理来源。每项都要
包含协议与端口，并以逗号分隔。此配置控制浏览器登录、携带会话的读取、
受 CSRF 保护的写入和 WebSocket 握手；它与 API 客户端使用的
`CORS_ORIGINS` 不同。浏览器会话使用现有 Redis，Redis 不可用时拒绝
会话。旧浏览器令牌迁移后用户需重新登录。HTTPS 入口的会话 Cookie
带 Secure；保留的局域网 HTTP 入口没有 HTTPS 传输保护，应限制在可信
网络或改用 HTTPS 反向代理。

## Resource limits · 资源上限

| Service | Memory / swap total | CPU | PIDs | OOM score |
| --- | ---: | ---: | ---: | ---: |
| PostgreSQL | 768M | 0.45 | 64 | 0 |
| Redis | 256M | 0.15 | 32 | 0 |
| Meilisearch | 1G | 0.40 | 64 | 300 |
| Migrate | 512M | 0.25 | 64 | 100 |
| Backend | unbounded | unbounded | 128 | 100 |
| Download worker | unbounded | unbounded | 96 | 500 |
| Import worker | unbounded | unbounded | 64 | 500 |
| Operations worker | unbounded | unbounded | 64 | 500 |
| Scheduler | unbounded | unbounded | 32 | 500 |
| Admin web | unbounded | unbounded | 64 | 100 |

`migrate` exits after applying migrations. Every service uses `json-file`
rotation (`10m`, 3 files). Download concurrency defaults to a deployment
ceiling of one; raising the UI setting alone cannot exceed the configured
limits. See [download worker concurrency](#download-worker-concurrency--下载并发).

Redis remains `192mb/noeviction` inside its protected 256M container because it
stores RQ queues as well as cache data. Its container health check performs a
short-lived `SET EX` and `DEL`, so a server that answers `PING` but rejects
writes is not considered healthy.

## Download worker concurrency · 下载并发

The download setting selects 1–5 consumers and takes effect after restarting
`worker-download`. Its effective count is the minimum of the persisted setting,
`DOWNLOAD_CONCURRENCY_CAP` (default 1), and the device download limit. Automatic
compact/standard/performance limits remain 1/1/2. An optional
`DOWNLOAD_DEVICE_CONCURRENCY_LIMIT` from 1–5 overrides only that device download
limit; leave it empty to retain automatic sizing. Queue and scheduler budgets
continue using the device profile.

The supervisor passes its effective count to child processes. Actual downloads
share that bounded slot pool: each slot reserves memory atomically with the
other resource profiles, and fixed POSIX locks retain the limit after Redis
coordination-key loss or a configuration decrease. Parallel downloads reserve
at least 256 MiB each; the single-slot path retains the existing profile reserve.
Low available memory can admit fewer downloads than configured. A consumer
count does not promise the same number of active gallery-dl processes.

Jobs from the same platform retain one archive lock because they share
`archive-{source}.sqlite3`. Archive identity lookup failures defer execution.
Cancellation, timeout and failure must stop the gallery-dl process group before
releasing the archive and execution slot. Downloads can coexist with media,
imports and remote indexing when their aggregate memory budget permits;
maintenance remains exclusive.

For an upgrade from the old single-token implementation, drain and stop old
download processes before starting the new version. Apply consistent limits and
code to the API and all Python worker roles; recreate them after changing
deployment limits. Changing the page setting still requires a download-worker
restart. The health response retains configured, effective and restart-required
values and identifies the automatic or explicit device limit.

Each consumer has its own database pool. Budget download connections as
`effective consumers × (WORKER_DB_POOL_SIZE + WORKER_DB_MAX_OVERFLOW)`, then add
API and other worker pools. Nested handoff sessions require at least two
connections per download consumer. Keep CPU, memory and PID limits appropriate
for the total consumer count, and select a count using the target device's
measured workload.

页面下载并发设置选择 1～5 个消费者，重启下载 worker 后生效。实际消费者数取
页面设置、`DOWNLOAD_CONCURRENCY_CAP`（默认 1）和设备下载上限的最小值。
自动 compact／standard／performance 档位仍为 1／1／2；可选的
`DOWNLOAD_DEVICE_CONCURRENCY_LIMIT`（1～5）仅覆盖设备的下载上限，留空继续
自动判断，不改变该档位的队列与调度预算。

下载进程使用有上限的执行名额，与其他任务原子核对总内存预留。固定文件锁在
Redis 协调键丢失、缩小配置时仍限制执行数；并行下载每个任务至少预留 256 MiB，
单名额沿用原 profile 预留。内存不足可以降低实际并行数，消费者数量不等于
gallery-dl 正在运行的数量。同平台任务共用归档，继续互斥；归档身份查询失败
时延后执行。取消、超时和失败先停止下载进程组，再释放归档锁与执行名额。

升级前先排空并停止旧下载进程，新代码和部署上限在 API 与全部 Python worker
中保持一致，修改部署配置后重新创建这些容器；页面设置仍按重启规则生效。
按上述公式核对总数据库连接，每个下载消费者至少提供两个连接用于嵌套交接会话，
同时核对 CPU、内存与 PID 配额，按目标设备实测选择并发数量。

## Image worker concurrency · 图片处理并行数

Set `MEDIA_WORKER_CONCURRENCY` in `.env` to an integer from 1 through 8; its
default is 1. Compose uses it for operations consumers, and the application uses
it for a shared ordinary/large-image execution pool. Apply the same value to the
API, Python workers and scheduler, then recreate those application containers.
Upload, download and import consumer counts are configured separately.

With more than one slot, each image reserves at least 768 MiB. The configured
count is a ceiling: resource pressure can admit fewer tasks, and video,
indexing, deduplication and maintenance retain their exclusive protection.
Choose the count using the device's time and peak-memory measurements.

Each operations consumer has its own database pool. Its combined connection
ceiling is `MEDIA_WORKER_CONCURRENCY × (WORKER_DB_POOL_SIZE +
WORKER_DB_MAX_OVERFLOW)`; include API and other worker pools in the database's
total budget. Worker library threads default to 1 through `VIPS_CONCURRENCY`,
`OPENBLAS_NUM_THREADS`, `OMP_NUM_THREADS` and `MKL_NUM_THREADS`, avoiding nested
thread pools competing with process concurrency.

在 `.env` 中将 `MEDIA_WORKER_CONCURRENCY` 设为 1～8 的整数，默认 1。
它同时控制 operations 消费者数量和普通图／超大图共用的执行池；API、Python
worker 和 scheduler 使用相同值，修改后重新创建这些应用容器。上传、下载、
导入消费者数量分别配置。

并行模式每张图片至少预留 768 MiB，配置值只是上限；内存压力可以降低实际
执行数量，其他受保护任务的互斥规则继续生效。按设备上的耗时与峰值内存选择
数量。

每个 operations 消费者都有独立数据库连接池，总连接上限按上述公式估算，
再加上 API 和其他 worker 的预算。内部库线程默认设为 1；提高进程数时同时
核对数据库连接、内存与库线程预算。

## Adaptive algorithm governance · 自适应算法治理

`RESOURCE_GOVERNANCE_MODE=enforce` is the default for non-Gitllery profiles.
The host memory reserve is device-relative: `clamp(MemTotal × 15%, 384 MiB,
2560 MiB)`, with a fixed override available. Active Swap collapse, unreadable
core metrics, project cgroup OOM events and Redis/disk hard failures stop only
auto-gallery heavy work. PSI alone only scales micro-batches through AIMD and
never turns the whole pipeline off.

非 Gitllery profile 默认启用 `RESOURCE_GOVERNANCE_MODE=enforce`。宿主储备按
`clamp(MemTotal × 15%, 384 MiB, 2560 MiB)` 自动标定，也可固定覆盖。硬风险只停止
auto-gallery 重任务；PSI 仅通过 AIMD 缩小微批，不会单独让流水线归零。

The profile controller separates network download, import DB batches, image
and video derivation, search indexing, Gitllery projection, and exclusive
maintenance. Import/search/Git/media release their permit after at most 25
works, 500/4 MiB documents, 25 changes, one asset, or 20 seconds. Resource
waiting must not hold an application transaction. Workers wake on controller
events and otherwise back off with jitter from 2 to 30 seconds; stable state is
written to Redis only every 30 seconds.

Normal downloads use `DOWNLOAD_ROOT/.staging/<job-id>` and register only that
job's manifest delta. A provider may opt into the cursor interface only after
its cursor is proven stable under remote inserts; all current providers retain
the safe complete `1-N` invocation and gallery-dl archive behaviour.

PostgreSQL loads `pg_stat_statements` with top-level controlled tracking. Search
and pipeline logs expose wall/CPU time, SQL and durable commit counts,
process-I/O bytes and peak RSS for each bounded slice.

### Promotion sequence · 启用顺序

1. Deploy with adaptive governance and `DOWNLOAD_STAGING_ENABLED=1`. Canary one small source and verify hard-link
   promotion, ledger rows and an empty completed stage.
2. Collect at least 24 hours of mixed vendor-service and auto-gallery traffic:

   ```bash
   python3 scripts/monitor-nas-resources.py --interval 10 --duration 86400 \
     > auto-gallery-governance-shadow.jsonl
   ```

   Confirm controller hard reasons match real hazards, cgroup OOM/restart
   counters stay flat, work continues under high external PSI, and the
   calculated scale falls within 30 seconds of foreground degradation.
3. Run `scripts/observe-core-shadow.sh`. It performs the required two-hour
   read/light-write coexistence observation without starting background
   workers. Continue shadow observation until the deployment has been live for
   24 hours.
4. Use `scripts/rollout-governance.sh import`, then `search`, then `download`.
   The script enforces the 24h/6h/6h minimum observation windows and starts
   only the workers required by the selected stage.
5. Gitllery remains product v1 and projection stays shadow-only for this
   rollout. Do not run full-history reconcile, backfill or rebuild. PostgreSQL
   curation commits and changes remain authoritative.
6. If Meilisearch cold reads still miss the gallery P95 target after the
   algorithm changes, move only its hot index volume to local SSD; keep dumps
   and backups on NAS storage.

Stop background workers if an auto-gallery cgroup records OOM, workers restart,
or core service health fails. Do not change host earlyoom or other projects as
part of auto-gallery resource governance.

## Optional NAS I/O throttle · 可选 NAS I/O 限速

`docker-compose.nas-io.yaml` adds cgroup-v2 bandwidth limits to download,
import, operations, and Meilisearch. It is opt-in because btrfs/RAID/device-
mapper stacks may charge I/O to a lower physical device than the mounted path.

1. Resolve the device backing the data bind mounts; do not infer it from a
   volume name:

   ```bash
   findmnt -T /volume2/docker/auto-gallery
   lsblk -o NAME,TYPE,FSTYPE,MOUNTPOINTS
   ```

2. Validate the merged configuration without starting or changing containers:

   ```bash
   NAS_BLOCK_DEVICE=/dev/sdb docker compose \
     -f docker-compose.yaml -f docker-compose.nas-io.yaml config --quiet
   ```

3. Apply only after confirming that `/dev/sdb` is the correct physical device:

   ```bash
   NAS_BLOCK_DEVICE=/dev/sdb docker compose \
     -f docker-compose.yaml -f docker-compose.nas-io.yaml up -d
   ```

4. Inspect each live container's cgroup `io.max` and run a controlled test.
   Retain the override only when observed throughput is within 20% of the
   configured limits. If the storage stack ignores it, remove the override;
   single concurrency plus inherited `nice -n 10`/`ionice -c2 -n7` remains the
   safe fallback.

## Safe rollout · 安全上线

1. For a local deployment run `scripts/deploy.sh`; existing failed acceptance
   state is ignored. Use `--core-only` when only browsing should start.
2. For a formal release run `scripts/run-acceptance.sh core`. The five-minute host PSI baseline is
   used for attribution, not as a standalone veto. Correctness, migration,
   frontend, fault and 30-minute core smoke checks must produce a signed
   checksummed `deployment_scope=core` acceptance manifest for the exact image
   digests.
3. Deploy the accepted images with `scripts/deploy.sh --verified <manifest>`.
   The script creates a checked rollback point, stops old worker publishers,
   migrates and recreates the foreground application, then waits for the new
   enforced controller to clear any inherited hard latch after its full stable
   recovery window. It never deletes the latch manually. Only after the core
   runtime check passes does it recreate adaptive workers (unless `--core-only`
   is supplied), then verify worker health and queue listeners. Idle queues and
   previews are valid, so rollout does not require a progress counter to move.
   Host metrics are observational; backup, migration, image identity and
   project health are fail-closed.
4. Observe the deployment before increasing any environment-overridden caps:
   starting the import rollout:

   ```bash
   scripts/observe-core-shadow.sh
   scripts/rollout-governance.sh import
   ```

   The JSONL includes host PSI and Swap trends, per-container memory/CPU/I/O,
   cgroup pressure/events, controller budgets, queue activity and outbox lag.
   Confirm protected services have equal memory and memory-swap limits,
   application services have no hard memory/swap/CPU quota, no new cgroup
   `oom_kill`, no restart-count growth, and no more than 256 MiB host Swap
   growth from the clean baseline.
5. Enable the NAS I/O override only after the device and cgroup-v2 throughput
   checks above pass, then repeat the mixed-load observation for 24–48 hours.
6. Governance never edits host earlyoom, Swap, system services, or another
   Compose project.
