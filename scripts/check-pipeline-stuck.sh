#!/usr/bin/env bash
# Read-only pipeline stuck check for the running docker-compose stack.
set -euo pipefail
cd "$(dirname "$0")/.."

env_file="${COMPOSE_ENV_FILE:-.env}"
stale_after="${STUCK_AFTER_SECONDS:-900}"

if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi

compose() {
  if [[ -f "$env_file" ]]; then
    docker compose --env-file "$env_file" "$@"
  else
    docker compose "$@"
  fi
}

echo "== Pipeline stuck check =="
echo "stale_after_seconds=$stale_after"

compose run --rm -T -v "$(pwd)/backend:/app" -e STUCK_AFTER_SECONDS="$stale_after" backend python - <<'PY'
import asyncio
import json
import os
import sys

from app.database import async_session
from app.services.pipeline_health import collect_pipeline_health
from app.services.redis_client import get_redis


async def main() -> int:
    async with async_session() as db:
        health = await collect_pipeline_health(
            db,
            redis_client=get_redis(),
            stale_after_seconds=int(os.environ.get("STUCK_AFTER_SECONDS", "900")),
        )
    print(json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if health["ok"] else 1


raise SystemExit(asyncio.run(main()))
PY

echo ""
echo "== RQ queues =="
compose run --rm -T -v "$(pwd)/backend:/app" backend sh -lc 'rq info --url "$REDIS_URL" downloads imports operations scheduled'
