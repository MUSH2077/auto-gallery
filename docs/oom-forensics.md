# NAS / container OOM forensics · NAS 与容器爆内存取证

auto-gallery's 8GB-NAS profile disables container swap and pauses background
work before the host reaches earlyoom territory. A host kill and a cgroup kill
are different failures: collect both sets of evidence before changing a limit.

auto-gallery 的 8GB NAS 配置会禁止容器使用 Swap，并在宿主机进入 earlyoom 区间前暂停
后台任务。宿主机 earlyoom 与容器 cgroup OOM 是两类故障，调整限额前应分别取证。

When the backend container OOMs on the NAS, collect the items below and paste
them back. With the logging fix, the backend now logs its RSS every minute and
WARNs before the OOM, and `GET /api/v1/admin/memory` returns a live object
census that names exactly what is filling RAM.

当 backend 容器在 NAS 上 OOM 时，收集下面的内容贴回来即可定位。日志修复后 backend 每分钟
记录 RSS 并在 OOM 前告警；`GET /api/v1/admin/memory` 返回实时对象普查，直接指出是什么占满内存。

## 1. Confirm it was an OOM kill · 确认是 OOM 击杀

```bash
docker inspect auto-gallery-backend-1 --format 'OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}} RestartCount={{.RestartCount}}'
```

`OOMKilled=true` / `ExitCode=137` = the kernel killed it for exceeding the
memory limit. 说明超限被内核杀。

## 2. The RSS climb + last activity from logs · 日志里的 RSS 爬升与最后活动

App logs now go to stdout, so `docker compose logs` captures them:

```bash
# last 300 lines around the crash
docker compose logs backend --tail 300 > backend-oom.log
# just the memory monitor lines (RSS over time)
docker compose logs backend | grep -iE "backend memory|high backend memory"
```

Look for `High backend memory: RSS=...MB` — the line just before the crash and
the requests/log lines around it show **what was running** when RAM spiked.
关注 `High backend memory` 出现前后的日志，它旁边的请求/日志行就是内存飙升时**正在跑什么**。

## 3. Object census — names the culprit · 对象普查（直接点名元凶）

Run this **while memory is high** (not after a restart). It needs an admin token:

```bash
# get a token (replace with your admin password)
TOKEN=$(curl -s -X POST http://localhost:8818/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"YOUR_ADMIN_PASSWORD"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s "http://localhost:8818/api/v1/admin/memory?top=30" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool > backend-memory.json
```

`backend-memory.json` shows `rss_mb` and the top live object types by count and
size. Read the top rows:
- `CurationChange` / `WorkSource` / `Work` in the **millions** → a full-table
  load (leading suspect: the gitllery status full-walk, or a reindex/backfill
  running inline). gitllery 全量兜底 / reindex / backfill。
- `Row` / `dict` / `tuple` growing across repeated snapshots → a leak (take two
  snapshots a few minutes apart and compare). 反复快照对比，持续增长=泄漏。

## 4. Container memory over time · 容器内存随时间

For the 24–48 hour acceptance window, run the host-side sampler. It records
host memory/Swap/PSI plus each auto-gallery container's cgroup-v2 current and
peak memory, `memory.events.oom_kill`, Docker OOM state and restart count. The
backend is deliberately not given the Docker socket.

在 24–48 小时验收窗口内，使用宿主机采样器持续记录宿主内存、Swap、PSI，以及每个
auto-gallery 容器的当前/峰值内存、`oom_kill` 和重启次数；应用容器不会获得 Docker Socket。

```bash
# one read-only sample
python3 scripts/monitor-nas-resources.py | python3 -m json.tool

# 24-hour JSONL evidence, one sample every 10 seconds
python3 scripts/monitor-nas-resources.py --interval 10 --duration 86400 \
  > auto-gallery-resource-soak.jsonl
```

```bash
docker stats --no-stream auto-gallery-backend-1
# or a short sample while reproducing:
for i in $(seq 1 20); do docker stats --no-stream --format '{{.Name}} {{.MemUsage}}' auto-gallery-backend-1; sleep 5; done
```

Inspect every auto-gallery cgroup rather than only the backend:

```bash
for id in $(docker compose ps -q); do
  docker inspect "$id" --format '{{.Name}} OOM={{.State.OOMKilled}} restarts={{.RestartCount}} memory={{.HostConfig.Memory}} memory_swap={{.HostConfig.MemorySwap}}'
done
```

`memory_swap` must equal `memory`. If it is twice the memory limit, the running
container predates the NAS resource profile and must be recreated during a
maintenance window.

## 5. Host pressure and earlyoom · 宿主机压力与 earlyoom

The resource controller is exposed in the authenticated system health response:

```bash
curl -s http://localhost:8818/api/v1/system/health \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool

free -h
cat /proc/pressure/memory
cat /proc/pressure/io
journalctl -u earlyoom --since "24 hours ago" | tail -n 200
```

Record `resource_pressure.status/reasons`, available memory, free-swap ratio,
and PSI. In the protected profile, queued heavy work pauses when memory or swap
crosses the hard threshold and resumes only after the configured hysteresis
window. Do not disable earlyoom to hide these warnings.

## 6. Safe swap recovery · 安全恢复 Swap

Never run `swapoff` while free RAM is smaller than used swap. Stop development
tools and heavy jobs first; on this class of NAS, use a planned reboot to obtain
a clean baseline. After restart, record `free -h`, then compare swap growth over
the next 24–48 hours. The acceptance target is no auto-gallery OOM/restart and
no more than 256 MiB swap growth attributable to its workload.

## 7. What were you doing? · 当时在做什么

Note the action that triggered it (opening a page, a search, a reindex, a
backfill, the gitllery panel, a backup…). Combined with the census + logs this
pins the root cause. 记下触发操作，配合普查+日志即可定位。

---

**What to paste back · 贴回这些**: `backend-oom.log`, `backend-memory.json`, the
system health pressure block, all `docker inspect` OOM/restart lines, relevant
earlyoom lines, and the action you were doing.
