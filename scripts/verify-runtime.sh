#!/usr/bin/env bash
set -euo pipefail

services=(
  postgres
  redis
  meilisearch
  backend
  admin-web
  worker-download
  worker-import
  scheduler
)

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

compose() {
  docker compose "$@"
}

require_service_ok() {
  local service="$1"
  local status health
  status="$(compose ps "$service" --format '{{.State}}')"
  health="$(compose ps "$service" --format '{{.Health}}')"
  if [[ "$status" != "running" ]]; then
    echo "Service $service is not running: $status" >&2
    return 1
  fi
  if [[ -n "$health" && "$health" != "healthy" ]]; then
    echo "Service $service is not healthy: $health" >&2
    return 1
  fi
  echo "ok: $service $status ${health:-no-healthcheck}"
}

for service in "${services[@]}"; do
  require_service_ok "$service"
done

compose exec -T backend curl -sf http://localhost:8000/api/v1/system/health >/dev/null
echo "ok: backend health endpoint"

compose exec -T admin-web wget -q -O /dev/null http://127.0.0.1:3000/
echo "ok: admin-web homepage"

head_revision="$(compose exec -T backend alembic heads | awk '{print $1}' | tail -n 1)"
current_revision="$(compose exec -T backend alembic current | awk '{print $1}' | tail -n 1)"
if [[ -z "$head_revision" || "$current_revision" != "$head_revision" ]]; then
  echo "Alembic revision mismatch: current=${current_revision:-none} head=${head_revision:-none}" >&2
  exit 1
fi
echo "ok: alembic current $current_revision"

compose exec -T redis redis-cli -a "${REDIS_PASSWORD:?REDIS_PASSWORD is required}" ping >/dev/null
echo "ok: redis ping"

compose exec -T backend python3 - <<'PY'
import os
from redis import Redis
from rq import Queue

connection = Redis.from_url(os.environ["REDIS_URL"])
for name in ("default", "downloads", "imports", "scheduled"):
    queue = Queue(name, connection=connection)
    len(queue)
    queue.scheduled_job_registry.count
print("ok: rq queues readable")
PY

echo "Runtime verification complete."
