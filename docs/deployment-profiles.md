# Deployment Profile · 部署配置

There is a single compose file: `docker-compose.yaml`. Its resource limits are
sized conservatively for an ~8GB-RAM NAS, so the same command works everywhere
(dev box and NAS alike):

只有一份 compose 文件：`docker-compose.yaml`。其资源上限按约 8GB 内存的 NAS 保守设定，
开发机与 NAS 使用同一条命令：

```bash
docker compose up -d
```

## Why conservative limits · 为什么取保守上限

Import thumbnailing (pyvips) and Meilisearch are the two biggest memory
spikes. Capping them keeps heavy tasks from pushing a small box into swap —
swap on spinning disks is what makes *everything* (including the frontend)
crawl.

导入缩略图（pyvips）与 Meilisearch 是两个最大的内存尖峰。压低上限可避免重任务把小内存
机器压进交换区；机械盘上的交换正是"一跑任务整个前端都变慢"的元凶。

## Current limits · 当前上限

| Service · 服务 | mem_limit | Notes · 说明 |
| --- | --- | --- |
| backend | 1g | raised from 768M (2026-07-07) — never actually OOM-killed, but felt tight under load · 从 768M 调高，未实际被 OOM 但负载下偏紧 |
| worker-download | 512M | |
| worker-import | 1g | single worker (`imports 1`) — pyvips burst ceiling · 单并发，压 pyvips 尖峰 |
| worker-operations | 512M | |
| scheduler | 256M | |
| admin-web | 256M | `NODE_OPTIONS --max-old-space-size=256` |
| meilisearch | 768M | + `MEILI_MAX_INDEXING_MEMORY=256Mb`, 2 indexing threads — raised from 512M after it ran at 76% RSS on only a 711-doc library · 从 512M 调高，711 篇作品的小库已跑到 76% |
| postgres | 384M | |
| redis | 128M | `--maxmemory 96mb --maxmemory-policy noeviction` |

Totals at limits ≈ 4.75G, leaving ~3.25G headroom for the NAS OS on an 8GB
machine. Diagnose with real evidence before raising further — check
`docker inspect <container> --format '{{.State.OOMKilled}}'` and
`dmesg | grep -i oom-kill` for which service actually hit its ceiling; don't
raise limits on a hunch. Removing limits entirely is deliberately avoided:
a memcg OOM kill is an isolated, auto-restarted failure (`restart:
unless-stopped`), while an unbounded runaway container on an 8GB box can
exhaust host RAM and take down every other service (postgres, redis) at once.

上限合计约 4.75G，8GB 机器上为系统留出约 3.25G 余量。调高限额前先找证据——用
`docker inspect <container> --format '{{.State.OOMKilled}}'` 和
`dmesg | grep -i oom-kill` 确认到底是哪个服务真被打满，不要凭感觉调。刻意不去掉
限额：memcg OOM kill 是隔离的、可自动重启的故障（`restart: unless-stopped`），而
8GB 机器上失控的无上限容器会耗尽整机内存，把 postgres/redis 等所有服务一起拖垮。

## Redis eviction policy · Redis 淘汰策略

Redis backs both the API cache **and** RQ job queues. `noeviction` is
required: evicting keys under memory pressure would silently drop queued
jobs — overflow must error loudly instead.

Redis 同时承载 API 缓存**和** RQ 任务队列。必须使用 `noeviction`：内存压力下淘汰键会
静默丢失排队任务——溢出必须显式报错。
