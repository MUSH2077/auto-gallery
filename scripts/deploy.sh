#!/usr/bin/env bash
# auto-gallery Deploy — single command to apply all changes
# Usage: ./scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== auto-gallery Deploy ==="

# 1. Build images with latest code
echo "[1/4] Building images..."
docker compose build backend admin-web

# 2. Force-recreate all containers
echo "[2/4] Restarting containers..."
docker compose up -d --force-recreate backend worker-download worker-import stream-import scheduler admin-web

# 3. Wait for healthy
echo "[3/4] Waiting for healthy..."
for i in $(seq 1 12); do
    unhealthy=$(docker ps --filter "name=auto-gallery" --filter "health=unhealthy" --format '{{.Names}}' 2>/dev/null | wc -l)
    starting=$(docker ps --filter "name=auto-gallery" --filter "health=starting" --format '{{.Names}}' 2>/dev/null | wc -l)
    if [ "$unhealthy" -eq 0 ] && [ "$starting" -eq 0 ]; then
        echo "  All healthy"
        break
    fi
    echo "  Waiting... ($unhealthy unhealthy, $starting starting)"
    sleep 5
done

# 4. Quick health summary
echo "[4/4] Health check..."
bash scripts/debug.sh quick

echo ""
echo "=== Deploy complete ==="
echo "  API:      http://localhost:8818"
echo "  Frontend: http://localhost:13000"
echo "  Debug:    ./scripts/debug.sh [backend|download|storage|proxy]"
echo "  Smoke:    ./scripts/smoke-test.sh"
