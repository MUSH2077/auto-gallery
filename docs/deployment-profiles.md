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
|---|---|---|
| backend | 768M | |
| worker-download | 512M | |
| worker-import | 1g | single worker (`imports 1`) — pyvips burst ceiling · 单并发，压 pyvips 尖峰 |
| worker-operations | 512M | |
| scheduler | 256M | |
| admin-web | 256M | `NODE_OPTIONS --max-old-space-size=256` |
| meilisearch | 256M | plenty for a small library · 小型库足够 |
| postgres | 384M | |
| redis | 128M | `--maxmemory 96mb --maxmemory-policy noeviction` |

Totals at limits ≈ 4.9G, leaving headroom for the NAS OS on an 8GB machine.
If a service is OOM-killed on a larger box, raise its `mem_limit` in
`docker-compose.yaml` directly.

上限合计约 4.9G，8GB 机器上为系统留出余量。更大内存的机器如遇 OOM，直接在
`docker-compose.yaml` 中调高对应服务的 `mem_limit` 即可。

## Redis eviction policy · Redis 淘汰策略

Redis backs both the API cache **and** RQ job queues. `noeviction` is
required: evicting keys under memory pressure would silently drop queued
jobs — overflow must error loudly instead.

Redis 同时承载 API 缓存**和** RQ 任务队列。必须使用 `noeviction`：内存压力下淘汰键会
静默丢失排队任务——溢出必须显式报错。
