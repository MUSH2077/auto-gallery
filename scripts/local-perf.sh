#!/usr/bin/env bash
# Run under Linux/WSL, so data stays on the Linux filesystem.
set -Eeuo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$PROJECT_ROOT"
PERF_ROOT="$PROJECT_ROOT/data/local-perf"
PERF_ENV="$PERF_ROOT/perf.env"
umask 077

initialize() {
  [[ ! -f "$PERF_ENV" ]] || return 0
  mkdir -p "$PERF_ROOT"
  python3 - "$PROJECT_ROOT" "$PERF_ROOT" <<'PY'
import base64
from pathlib import Path
import secrets
import subprocess
import sys

repo, root = map(Path, sys.argv[1:])
revision = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip()
dirty = subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()
values = {
    "COMPOSE_PROJECT_NAME": "auto-gallery-perf",
    "BACKEND_IMAGE": "auto-gallery-backend:local-perf",
    "ADMIN_IMAGE": "auto-gallery-admin-web:local-perf",
    "BUILD_REVISION": f"local-perf-{revision}" + ("-working-tree" if dirty else ""),
    "POSTGRES_PASSWORD": secrets.token_hex(24),
    "REDIS_PASSWORD": secrets.token_hex(24),
    "MEILI_MASTER_KEY": secrets.token_hex(24),
    "SECRET_KEY": secrets.token_hex(32),
    "REMOTE_CREDENTIAL_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    "ADMIN_PASSWORD": secrets.token_urlsafe(18),
    "BACKEND_PORT": "18818",
    "ADMIN_WEB_PORT": "23000",
    "TIMEZONE": "Asia/Shanghai",
    "RESOURCE_GOVERNANCE_MODE": "enforce",
    "POSTGRES_MEM_LIMIT": "384M",
    "REDIS_MEM_LIMIT": "128M",
    "MEILI_MEM_LIMIT": "512M",
    "POSTGRES_CPU_LIMIT": "2.0",
    "REDIS_CPU_LIMIT": "0.5",
    "MEILI_CPU_LIMIT": "2.0",
    "MIGRATE_CPU_LIMIT": "1.0",
    "ADMIN_BUILD_NODE_HEAP_MB": "2048",
    "NEXT_PUBLIC_WS_URL": "ws://localhost:18818/api/v1/ws",
}
for key, directory in {
    "HOST_POSTGRES": "postgres", "HOST_REDIS": "redis",
    "HOST_MEILISEARCH": "meilisearch", "HOST_DOWNLOADS": "downloads",
    "HOST_LIBRARY": "library", "HOST_CONFIG_APP": "app-config",
    "HOST_CONFIG_GALLERYDL": "gallery-dl", "HOST_RESTORE_STAGING": "restore-staging",
    "HOST_RESTORE_RECEIPTS": "restore-receipts",
}.items():
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)
    values[key] = str(path)
lines = []
for line in (repo / ".env.example").read_text().splitlines():
    key = line.split("=", 1)[0]
    if not line.startswith("#") and key in values:
        line = f"{key}={values.pop(key)}"
    lines.append(line)
lines.extend(f"{key}={value}" for key, value in values.items())
env = root / "perf.env"
env.write_text("\n".join(lines) + "\n")
env.chmod(0o600)
PY
  echo "Created $PERF_ENV (admin password stored here)."
}

compose() {
  local worker_profile=()
  if [[ "${AUTO_GALLERY_PERF_BASE_ONLY:-1}" != 1 && -f "$PERF_ROOT/workers.compose.json" ]]; then
    worker_profile=(-f "$PERF_ROOT/workers.compose.json")
  fi
  docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery-perf \
    --env-file "$PERF_ENV" -f docker-compose.yaml -f docker-compose.perf.yaml "${worker_profile[@]}" "$@"
}

action="${1:-status}"
[[ $# -eq 0 ]] || shift
case "$action" in
  init) initialize ;;
  build) initialize; compose build backend admin-web "$@" ;;
  up) initialize; compose up -d --wait --wait-timeout 300 "$@" ;;
  seed) compose run --rm --no-deps --user "$(id -u):$(id -g)" -v "$PERF_ROOT:/perf" \
    -v "$PROJECT_ROOT/scripts/local-perf-data.py:/tmp/local-perf-data.py:ro" \
    backend python /tmp/local-perf-data.py "$@" ;;
  stop) compose stop "$@" ;;
  status) compose ps -a "$@" ;;
  logs) compose logs --tail 100 "$@" ;;
  benchmark) python3 scripts/local-perf-benchmark.py "$@" ;;
  compose) compose "$@" ;;
  *) echo "Usage: bash scripts/local-perf.sh init|build|up|seed|benchmark|stop|status|logs|compose [args]" >&2; exit 2 ;;
esac
