# Deployment Profile · 部署配置

The base `docker-compose.yaml` is a portable project-local profile. Every
memory, CPU and PID ceiling has an environment override, and auto-gallery
containers cannot consume host swap (`memswap_limit == mem_limit`). These are
upper fuses rather than host reservations. Lower worker I/O priority, bounded
logs and runtime micro-batching keep background work cooperative.

基础 `docker-compose.yaml` 是可移植的项目级配置。每个容器的内存、CPU 和 PID
上限都可由环境变量覆盖；auto-gallery 容器不能使用宿主机 Swap
(`memswap_limit == mem_limit`)。这些值是保险丝，不是从宿主预留的资源。

Ordinary local deployment builds a source-digest candidate. Formal releases use
`scripts/deploy.sh --verified <manifest>` and never rebuild accepted images.
Host pressure is recorded, not used as a device-specific deployment veto.

Database migrations run in the one-shot `migrate` service. Backend and workers
wait for it to complete, while Meilisearch is deliberately not a backend startup
dependency: an unavailable search index must degrade search, not take down the
whole application.

## Resource limits · 资源上限

| Service | Memory / swap total | CPU | PIDs | OOM score |
| --- | ---: | ---: | ---: | ---: |
| PostgreSQL | 384M | 0.45 | 64 | 0 |
| Redis | 128M | 0.15 | 32 | 0 |
| Meilisearch | 512M | 0.40 | 64 | 300 |
| Backend | 512M | 0.55 | 128 | 100 |
| Download worker | 320M | 0.35 | 96 | 500 |
| Import worker | 640M | 0.65 | 64 | 500 |
| Operations worker | 384M | 0.25 | 64 | 500 |
| Scheduler | 128M | 0.10 | 32 | 500 |
| Admin web | 256M | 0.15 | 64 | 100 |

`migrate` has a transient 256M limit and exits after applying migrations. Every
service uses `json-file` rotation (`10m`, 3 files). Download concurrency has a
deployment ceiling of one; raising the UI setting alone cannot exceed it.

Redis remains `96mb/noeviction` because it stores RQ queues as well as cache
data. Its container health check performs a short-lived `SET EX` and `DEL`, so a
server that answers `PING` but rejects writes is not considered healthy.

## Adaptive algorithm governance · 自适应算法治理

`RESOURCE_GOVERNANCE_MODE=enforce` is the default for non-Gitllery profiles.
The host memory reserve is device-relative: `clamp(MemTotal × 15%, 384 MiB,
1280 MiB)`, with a fixed override available. Active Swap collapse, unreadable
core metrics, project cgroup OOM events and Redis/disk hard failures stop only
auto-gallery heavy work. PSI alone only scales micro-batches through AIMD and
never turns the whole pipeline off.

非 Gitllery profile 默认启用 `RESOURCE_GOVERNANCE_MODE=enforce`。宿主储备按
`clamp(MemTotal × 15%, 384 MiB, 1280 MiB)` 自动标定，也可固定覆盖。硬风险只停止
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
   The script creates a checked rollback point, migrates, verifies the core,
   then starts adaptive workers unless `--core-only` is supplied. Host metrics
   are observational; backup, migration, image identity and project health are
   fail-closed.
4. Observe the deployment before increasing any environment-overridden caps:
   starting the import rollout:

   ```bash
   scripts/observe-core-shadow.sh
   scripts/rollout-governance.sh import
   ```

   The JSONL includes host PSI and Swap trends, per-container memory/CPU/I/O,
   cgroup pressure/events, controller budgets, queue activity and outbox lag.
   Confirm every container has equal memory and memory-swap limits, no new
   cgroup `oom_kill`, no restart-count growth, and no more than 256 MiB host
   Swap growth from the clean baseline.
5. Enable the NAS I/O override only after the device and cgroup-v2 throughput
   checks above pass, then repeat the mixed-load observation for 24–48 hours.
6. Governance never edits host earlyoom, Swap, system services, or another
   Compose project.
