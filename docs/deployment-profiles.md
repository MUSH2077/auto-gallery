# Deployment Profiles · 部署配置档

## 8GB NAS profile · 8GB 内存 NAS 档

**When to use · 适用场景**

Use this profile when the NAS has ~8GB RAM (or less headroom after the NAS OS).
It caps burst memory — import thumbnailing (pyvips) and Meilisearch are the two
biggest spikes — so heavy tasks can't push the machine into swap, which is what
makes *everything* (including the frontend) crawl on spinning disks.

当 NAS 只有约 8GB 内存（扣除系统后余量有限）时使用。该档压低突发内存上限——导入缩略图
（pyvips）与 Meilisearch 是两个最大的内存尖峰——避免重任务把机器压进交换区；机械盘上的
交换正是"一跑任务整个前端都变慢"的元凶。

**Usage · 用法**

```bash
docker compose -f docker-compose.yaml -f docker-compose.nas8g.yaml up -d
```

(All other compose commands take the same pair of `-f` flags, e.g. `build`,
`logs`. 其余 compose 命令同样带这两个 `-f` 参数。)

**What it changes · 改了什么**

| Service · 服务 | Base · 基础档 | nas8g |
|---|---|---|
| backend | 1024M | 768M |
| worker-download | 768M | 512M |
| worker-import | 2g / `imports 2` | **1g / `imports 1`**（单并发） |
| meilisearch | unlimited · 无限制 | **256M** |

App-service ceilings sum to ≈3.3G, leaving room for postgres (384M), redis
(128M), admin-web (256M), scheduler (256M) and the NAS OS.
应用服务上限合计约 3.3G，给 postgres/redis/admin-web/scheduler 与 NAS 系统留出余量。

**Related base-file change · 相关基础档变更**

The base compose now runs redis with `--maxmemory-policy noeviction` (was
`allkeys-lru`): this redis also backs the RQ job queues, and LRU eviction under
memory pressure would silently delete queued jobs. With noeviction, overflow
fails loudly instead.
基础 compose 的 redis 改为 `noeviction`（原 `allkeys-lru`）：该 Redis 同时承载 RQ 任务
队列，内存吃紧时 LRU 会静默删除排队任务；noeviction 让溢出变成显式报错。
