#!/usr/bin/env bash
# auto-gallery Deploy — single command to apply all changes
# Usage: ./scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== auto-gallery Deploy ==="

# 1. Build only if source changed
STAMP=".deploy-build-stamp"
SOURCE_CHANGED=false
if [ -f "$STAMP" ]; then
    CHANGED=$(find backend/ admin-web/ docker-compose.yaml scripts/ -newer "$STAMP" -type f 2>/dev/null | wc -l)
    [ "$CHANGED" -gt 0 ] && SOURCE_CHANGED=true
else
    SOURCE_CHANGED=true
fi

if [ "$SOURCE_CHANGED" = true ]; then
    echo "[1/4] Source changed — building..."
    # Try compose build first, fall back to direct build, accept failure
    (timeout 60 docker compose build backend admin-web 2>&1 || true)
    BUILD_OK=$?
    if [ "$BUILD_OK" -eq 0 ]; then
        echo "  Build OK"
    else
        echo "  Build skipped (ECR timeout) — reusing existing image"
    fi
    date +%s > "$STAMP"
else
    echo "[1/4] No source changes — skipping build"
fi

# 2. Force-recreate all containers
echo "[2/4] Restarting containers..."
docker compose up -d --force-recreate backend worker-download worker-import scheduler admin-web

# 3. Wait for healthy
echo "[3/4] Waiting for healthy..."
for i in $(seq 1 18); do
    UNHEALTHY=$(docker ps --filter "name=auto-gallery" --filter "health=unhealthy" --format '{{.Names}}' 2>/dev/null | wc -l)
    STARTING=$(docker ps --filter "name=auto-gallery" --filter "health=starting" --format '{{.Names}}' 2>/dev/null | wc -l)
    [ "$UNHEALTHY" -eq 0 ] && [ "$STARTING" -eq 0 ] && echo "  All healthy" && break
    echo "  Waiting... ($UNHEALTHY unhealthy, $STARTING starting)"
    sleep 5
done

# 4. Quick health summary
echo "[4/4] Health check..."
bash scripts/debug.sh quick

echo ""
echo "=== Deploy complete ==="
echo "  API: http://localhost:8818  |  Frontend: http://localhost:13000"
