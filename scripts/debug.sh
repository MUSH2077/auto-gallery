#!/usr/bin/env bash
# auto-gallery Debug Toolkit — reusable, granular
# Usage:
#   ./scripts/debug.sh              # Full system debug
#   ./scripts/debug.sh backend      # Backend deep dive (API, DB, logs)
#   ./scripts/debug.sh download     # Download pipeline (queues, FileIndex, gallery-dl)
#   ./scripts/debug.sh storage      # Storage + FileIndex breakdown
#   ./scripts/debug.sh proxy        # Proxy connectivity + DNS
#   ./scripts/debug.sh quick        # Quick health summary only
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; NC='\033[0m'
DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINERS=(backend worker-download worker-import stream-import scheduler admin-web postgres redis meilisearch)
OK=0; WARN=0; ERR=0

header(){ echo -e "\n${BOLD}${CYAN}═══ $1 ═══${NC}"; }
ok(){    echo -e "  ${GREEN}✅ $1${NC}"; OK=$((OK+1)); }
warn(){  echo -e "  ${YELLOW}⚠️  $1${NC}"; WARN=$((WARN+1)); }
err(){   echo -e "  ${RED}❌ $1${NC}"; ERR=$((ERR+1)); }

echo -e "\n${BOLD}auto-gallery Debug — $(date '+%H:%M:%S')${NC}"

# ═══ 1. Container Status ═══
header "Container Status"
for c in "${CONTAINERS[@]}"; do
    name="auto-gallery-${c}-1"
    st=$(docker inspect "$name" --format '{{.State.Status}}' 2>/dev/null || echo "missing")
    hc=$(docker inspect "$name" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' 2>/dev/null)
    ec=$(docker inspect "$name" --format '{{.State.ExitCode}}' 2>/dev/null)
    ts=$(docker inspect "$name" --format '{{.State.StartedAt}}' 2>/dev/null | cut -d. -f1 | sed 's/T/ /')
    # Docker returns UTC; convert to local time for display
    if command -v date >/dev/null 2>&1 && [ -n "$ts" ]; then
        ts=$(date -d "$ts UTC" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "$ts")
    fi
    case "$st-$hc" in
        running-healthy) ok    "$c: up (healthy) $ts" ;;
        running-unhealthy) err "$c: up (UNHEALTHY) exit=$ec $ts" ;;
        running-)         ok    "$c: up $ts" ;;
        exited-*)         err   "$c: EXITED code=$ec" ;;
        *)                warn  "$c: $st $hc" ;;
    esac
done

# ═══ 1b. Unhealthy deep dive ═══
for c in "${CONTAINERS[@]}"; do
    name="auto-gallery-${c}-1"
    hc=$(docker inspect "$name" --format '{{.State.Health.Status}}' 2>/dev/null)
    [ "$hc" != "unhealthy" ] && continue
    header "Health Check Failures: $c"
    docker inspect "$name" --format '{{range .State.Health.Log}}  [{{.Start | truncatechars 19}}] exit={{.ExitCode}} {{.Output | printf "%.120s"}}
{{end}}' 2>/dev/null | tail -5
done

# ═══ 2. Error Logs ═══
header "Recent Errors (last 5 per container)"
for c in backend worker-download worker-import stream-import scheduler; do
    name="auto-gallery-${c}-1"
    n=$(docker logs "$name" --tail 100 2>&1 | grep -ciE 'error|exception|traceback|timeout|stalled|failed' || true)
    [ "$n" -eq 0 ] && { ok "$c: clean"; continue; }
    warn "$c: $n errors in last 100 lines"
    docker logs "$name" --tail 100 2>&1 | grep -iE 'error|exception|traceback|timeout|stalled|failed' | tail -3 | sed 's/^/    /'
done

# ═══ 3. API ═══
header "API Readiness"
r=$(curl -sf "http://localhost:8818/api/v1/system/health" 2>&1) || true
echo "$r" | grep -q '"status".*"ok"' && ok "API healthy" || err "API unreachable: ${r:0:120}"

# ═══ 4. Storage ═══
header "Storage"
echo "  project: $(du -sh "$DIR/data" 2>/dev/null | cut -f1)"
echo "  downloads: $(du -sh "$DIR/data/downloads" 2>/dev/null | cut -f1)"
echo "  library: $(du -sh "$DIR/data/library" 2>/dev/null | cut -f1)"
fi="$DIR/data/downloads/.file-index.sqlite3"
if [ -f "$fi" ]; then
    n=$(sqlite3 "$fi" "SELECT COUNT(*) FROM file_index" 2>/dev/null || echo "?")
    ok "FileIndex: $(du -h "$fi" | cut -f1), $n entries"
fi

# ═══ Summary ═══
echo -e "\n${BOLD}Result:${NC} ${GREEN}$OK ok${NC}  ${YELLOW}$WARN warn${NC}  ${RED}$ERR err${NC}\n"

# ═══════════════════════════════════════════════
# Per-container deep dive modes
# ═══════════════════════════════════════════════
MODE="${1:-}"

backend_dive() {
    header "Backend: DB + Redis ping"
    docker exec auto-gallery-backend-1 python3 -c "
import asyncio; from app.database import async_session; from sqlalchemy import text
async def p():
    async with async_session() as db:
        r=await db.execute(text('SELECT 1')); print('  PG:', r.scalar())
    from app.services.redis_client import get_redis
    print('  Redis:', get_redis().ping())
asyncio.run(p())
" 2>&1
    echo "  Recent logs:"
    docker logs auto-gallery-backend-1 --tail 20 2>&1 | grep -v "healthcheck\|GET /api" | tail -10
}

download_dive() {
    header "Download: Queues + FileIndex + gallery-dl"
    docker exec auto-gallery-worker-download-1 python3 -c "
from app.services.redis_client import get_redis
from rq import Queue
import sqlite3
r=get_redis()
for qn in ['downloads:pixiv','downloads:danbooru','downloads:iwara']:
    q=Queue(name=qn,connection=r)
    if q.count>0 or len(q.started_job_registry)>0:
        print(f'  {qn}: waiting={q.count} active={len(q.started_job_registry)}')
conn=sqlite3.connect('/downloads/.file-index.sqlite3')
for row in conn.execute('SELECT source,import_status,COUNT(*) FROM file_index GROUP BY source,import_status'):
    print(f'  FileIndex: {row[0]} {row[1]}={row[2]}')
conn.close()
" 2>&1
    docker logs auto-gallery-worker-download-1 --tail 20 2>&1 | grep -iE 'gallery.dl|timeout|stalled|published|Registered' | tail -5
}

storage_dive() {
    header "Storage + FileIndex detail"
    echo "  Downloads:"
    ls -la "$DIR/data/downloads/" 2>/dev/null | head -8
    echo "  Library:"
    ls -la "$DIR/data/library/" 2>/dev/null | head -8
    [ -f "$fi" ] && sqlite3 "$fi" "SELECT file_type,import_status,COUNT(*) FROM file_index GROUP BY file_type,import_status" 2>/dev/null
}

proxy_dive() {
    header "Proxy health + DNS"
    docker exec auto-gallery-worker-download-1 python3 -c "
from app.services.redis_client import get_redis; r=get_redis()
for s in ['pixiv','danbooru','iwara','weibo','bilibili','pinterest','lofter']:
    k=f'proxy:health:{s}'
    d=r.hgetall(k) if r.exists(k) else {}
    st=d.get(b'status',b'?').decode()
    w=d.get(b'warnings',b'').decode()[:80]
    print(f'  {s}: {st} {w}')
import socket
for h in ['www.pixiv.net','host.docker.internal']:
    try:
        ip=socket.getaddrinfo(h,443,proto=socket.IPPROTO_TCP)[0][4][0]; print(f'  DNS {h} -> {ip}')
    except Exception as e: print(f'  DNS {h} -> FAIL: {e}')
" 2>&1
}

case "$MODE" in
    backend)  backend_dive ;;
    download) download_dive ;;
    storage)  storage_dive ;;
    proxy)    proxy_dive ;;
    quick)    ;;  # summary already shown
    "")       ;;  # summary only
    *)        echo "Unknown mode: $MODE. Try: backend download storage proxy quick" ;;
esac
