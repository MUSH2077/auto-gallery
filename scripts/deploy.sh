#!/usr/bin/env bash
# auto-gallery Deploy — build + restart + verify
# Usage: ./scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

echo -e "${GREEN}=== auto-gallery Deploy ===${NC}"

# ── 1. Build ──────────────────────────────────────────────────────────
echo -e "${YELLOW}[1/4] Building images...${NC}"
docker compose build \
    --build-arg CACHEBUST="$(date +%s)" \
    --pull \
    backend admin-web
echo -e "${GREEN}  Build OK${NC}"

# ── 2. Restart ────────────────────────────────────────────────────────
echo -e "${YELLOW}[2/4] Restarting containers...${NC}"
docker compose up -d --force-recreate \
    backend \
    worker-download \
    worker-import \
    worker-operations \
    scheduler \
    admin-web
echo -e "${GREEN}  Containers restarted${NC}"

# ── 3. Wait for healthy ───────────────────────────────────────────────
echo -e "${YELLOW}[3/4] Waiting for healthy...${NC}"
MAX_WAIT=90
for i in $(seq 1 $((MAX_WAIT / 5))); do
    UNHEALTHY=$(docker ps --filter "name=auto-gallery" --filter "health=unhealthy" --format '{{.Names}}' 2>/dev/null | wc -l)
    STARTING=$(docker ps --filter "name=auto-gallery" --filter "health=starting" --format '{{.Names}}' 2>/dev/null | wc -l)
    if [ "$UNHEALTHY" -eq 0 ] && [ "$STARTING" -eq 0 ]; then
        echo -e "${GREEN}  All healthy${NC}"
        break
    fi
    echo "  Waiting... ($UNHEALTHY unhealthy, $STARTING starting)  [${i}s]"
    sleep 5
done

# ── 4. Verify ─────────────────────────────────────────────────────────
echo -e "${YELLOW}[4/4] Health check...${NC}"
bash scripts/debug.sh quick

echo ""
echo -e "${GREEN}=== Deploy complete ===${NC}"
echo "  API:    http://localhost:8818"
echo "  Admin:  http://localhost:13000"
