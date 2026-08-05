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

env_file="${COMPOSE_ENV_FILE:-.env}"

if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1091
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

compose exec -T backend curl -sf http://localhost:8000/api/v1/system/ready >/dev/null
echo "ok: backend readiness endpoint"

compose exec -T backend curl -sf http://localhost:8000/api/v1/system/health >/dev/null
echo "ok: backend health endpoint"

docs_anonymous_status="$(compose exec -T backend curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/api/openapi.json)"
if [[ "$docs_anonymous_status" != "401" ]]; then
  echo "Protected OpenAPI endpoint returned $docs_anonymous_status to an anonymous client" >&2
  exit 1
fi
echo "ok: API contract rejects anonymous access"

compose exec -T backend python3 - <<'PY'
import asyncio

import httpx
from sqlalchemy import select

from app.auth import create_access_token
from app.database import async_session
from app.models.user import User


async def verify_docs() -> None:
    async with async_session() as db:
        username = (
            await db.execute(select(User.username).where(User.is_admin.is_(True)).limit(1))
        ).scalar_one()
    token = create_access_token(username, must_change_password=False)
    async with httpx.AsyncClient(
        base_url="http://admin-web:3000",
        cookies={"ag_token": token},
        timeout=20,
    ) as client:
        for path in (
            "/api/docs",
            "/api/redoc",
            "/api/openapi.json",
            "/api/asyncapi.yaml",
            "/api-docs/swagger-ui-bundle.js",
            "/api-docs/redoc.standalone.js",
        ):
            response = await client.get(path)
            response.raise_for_status()
        for legacy, canonical in (
            ("/docs", "/api/docs"),
            ("/redoc", "/api/redoc"),
            ("/openapi.json", "/api/openapi.json"),
        ):
            response = await client.get(legacy)
            if response.status_code != 308 or response.headers.get("location") != canonical:
                raise RuntimeError(
                    f"Legacy API docs redirect {legacy} returned "
                    f"{response.status_code} -> {response.headers.get('location')}"
                )
        business = await client.get("/api/v1/system/storage")
        if business.status_code != 401:
            raise RuntimeError(
                f"Business API accepted cookie-only authentication ({business.status_code})"
            )


asyncio.run(verify_docs())
PY
echo "ok: authenticated offline API documentation"

compose exec -T admin-web wget -q -O /dev/null http://127.0.0.1:3000/
echo "ok: admin-web homepage"

head_revision="$(compose exec -T backend alembic heads | awk '{print $1}' | tail -n 1)"
current_revision="$(compose exec -T backend alembic current | awk '{print $1}' | tail -n 1)"
if [[ -z "$head_revision" || "$current_revision" != "$head_revision" ]]; then
  echo "Alembic revision mismatch: current=${current_revision:-none} head=${head_revision:-none}" >&2
  exit 1
fi
echo "ok: alembic current $current_revision"

compose exec -T -e REDISCLI_AUTH="${REDIS_PASSWORD:?REDIS_PASSWORD is required}" redis redis-cli ping >/dev/null
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
