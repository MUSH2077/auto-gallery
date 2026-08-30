#!/usr/bin/env bash
set -euo pipefail

verify_scope="${VERIFY_SCOPE:-full}"
case "$verify_scope" in
  core)
    services=(postgres redis meilisearch backend admin-web)
    application_services=(backend admin-web)
    ;;
  import)
    services=(postgres redis meilisearch backend admin-web worker-import worker-operations)
    application_services=(backend admin-web worker-import worker-operations)
    ;;
  full)
    services=(postgres redis meilisearch backend admin-web worker-download worker-import worker-operations scheduler)
    application_services=(backend admin-web worker-download worker-import worker-operations scheduler)
    ;;
  *)
    echo "Unsupported VERIFY_SCOPE: $verify_scope" >&2
    exit 2
    ;;
esac
protected_services=(postgres redis meilisearch migrate)
resources_only="${VERIFY_RESOURCES_ONLY:-0}"

env_file="${COMPOSE_ENV_FILE:-.env}"
default_enforced_profiles="download_network,import_db,image_derive,video_derive,search_index,maintenance"
expected_enforced_profiles="${VERIFY_EXPECT_ENFORCED_PROFILES-$default_enforced_profiles}"

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

if [[ "$resources_only" != "1" ]]; then
  for service in "${services[@]}"; do
    require_service_ok "$service"
  done
fi

verify_resource_limits() {
  local service="$1" resource_class="$2"
  local container_id actual
  local memory memory_swap nano_cpus pids oom_score oom_killed restart_count

  if [[ "$service" == "migrate" ]]; then
    container_id="$(compose ps -a -q "$service")"
  else
    container_id="$(compose ps -q "$service")"
  fi
  if [[ -z "$container_id" ]]; then
    echo "Service $service has no container to inspect" >&2
    return 1
  fi
  actual="$(docker inspect "$container_id" --format \
    '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}} {{.HostConfig.OomScoreAdj}} {{.State.OOMKilled}} {{.RestartCount}}')"
  read -r memory memory_swap nano_cpus pids oom_score oom_killed restart_count <<<"$actual"

  case "$resource_class" in
    protected)
      if [[ "$memory" -le 0 || "$memory_swap" != "$memory" ]]; then
        echo "Protected service $service must have a positive memory limit with container Swap disabled: $memory/$memory_swap" >&2
        return 1
      fi
      if [[ "$nano_cpus" -le 0 ]]; then
        echo "Protected service $service must have a positive CPU limit: $nano_cpus" >&2
        return 1
      fi
      ;;
    application)
      if [[ "$memory" != "0" || "$memory_swap" != "0" || "$nano_cpus" != "0" ]]; then
        echo "Application service $service must have unlimited memory/Swap/CPU: $memory/$memory_swap/$nano_cpus" >&2
        return 1
      fi
      ;;
    *)
      echo "Unknown resource class for $service: $resource_class" >&2
      return 2
      ;;
  esac
  if [[ "$pids" -lt 16 ]]; then
    echo "Service $service has an invalid PID limit: $pids" >&2
    return 1
  fi
  if [[ "$oom_score" -lt -1000 || "$oom_score" -gt 1000 ]]; then
    echo "Service $service has an invalid oom_score_adj: $oom_score" >&2
    return 1
  fi
  if [[ "$oom_killed" != "false" || "$restart_count" != "0" ]]; then
    echo "Service $service has OOM/restart evidence: OOMKilled=$oom_killed RestartCount=$restart_count" >&2
    return 1
  fi

  if [[ "$service" != "migrate" ]]; then
    compose exec -T "$service" sh -c \
      'if [ -r /sys/fs/cgroup/memory.events ]; then test "$(awk '\''$1 == "oom_kill" {print $2}'\'' /sys/fs/cgroup/memory.events)" = "0"; fi'
  fi
  echo "ok: $service $resource_class resource contract and OOM counters"
}

for service in "${protected_services[@]}"; do
  verify_resource_limits "$service" protected
done
for service in "${application_services[@]}"; do
  verify_resource_limits "$service" application
done

if [[ "$resources_only" == "1" ]]; then
  echo "Runtime resource verification complete."
  exit 0
fi

compose exec -T backend curl -sf http://localhost:8000/api/v1/system/ready >/dev/null
echo "ok: backend readiness endpoint"

compose exec -T backend curl -sf http://localhost:8000/api/v1/system/health >/dev/null
echo "ok: backend health endpoint"

compose exec -T backend sh -c \
  'for path in /downloads /library /app-config /gallerydl-config; do test -d "$path" && test -w "$path"; done'
echo "ok: project bind targets are present and writable"

compose exec -T \
  -e VERIFY_REQUIRE_NORMAL_PRESSURE="${VERIFY_REQUIRE_NORMAL_PRESSURE:-0}" \
  -e VERIFY_ALLOW_CRITICAL_PRESSURE="${VERIFY_ALLOW_CRITICAL_PRESSURE:-0}" \
  -e VERIFY_EXPECT_GOVERNANCE_MODE="${VERIFY_EXPECT_GOVERNANCE_MODE:-enforce}" \
  -e VERIFY_EXPECT_ENFORCED_PROFILES="$expected_enforced_profiles" \
  -e VERIFY_EXPECT_MAX_SCALE="${VERIFY_EXPECT_MAX_SCALE:-1.0}" \
  backend python3 - <<'PY'
import httpx
import os

response = httpx.get("http://localhost:8000/api/v1/system/health", timeout=20)
response.raise_for_status()
health = response.json()
pressure = health["resource_pressure"]
redis = pressure["redis"]
concurrency = pressure["download_concurrency"]

require_normal = os.environ.get("VERIFY_REQUIRE_NORMAL_PRESSURE", "0") == "1"
allow_critical = os.environ.get("VERIFY_ALLOW_CRITICAL_PRESSURE", "0") == "1"
if require_normal:
    assert pressure["status"] == "normal", pressure
    assert not pressure.get("reasons"), pressure
elif allow_critical:
    assert pressure["status"] in {"normal", "warning", "paused"}, pressure
    assert pressure.get("controller_mode") in {"normal", "constrained", "critical"}, pressure
else:
    assert pressure["status"] in {"normal", "warning"}, pressure
    assert pressure.get("controller_mode") in {"normal", "constrained"}, pressure
controller = pressure.get("controller") or {}
expected_mode = os.environ["VERIFY_EXPECT_GOVERNANCE_MODE"]
expected_profiles = {
    item.strip()
    for item in os.environ.get("VERIFY_EXPECT_ENFORCED_PROFILES", "").split(",")
    if item.strip()
}
assert controller.get("governance_mode") == expected_mode, controller
assert set(controller.get("enforced_profiles") or []) == expected_profiles, controller
assert abs(float(controller.get("rollout_max_scale")) - float(os.environ["VERIFY_EXPECT_MAX_SCALE"])) < 0.0001, controller
calibration = controller.get("device_calibration") or {}
assert calibration.get("memory_reserve_mode") in {"auto", "fixed"}, calibration
assert int(calibration.get("memory_reserve_bytes") or 0) > 0, calibration
assert redis["writable"] is True, redis
assert float(redis["usage_ratio"]) < 0.90, redis
assert concurrency["cap"] == 1, concurrency
assert concurrency["effective"] == 1, concurrency
assert concurrency["restart_required"] is False, concurrency
assert set(pressure["queue_activity"]) >= {
    "downloads",
    "imports",
    "operations",
    "scheduled",
}, pressure["queue_activity"]
PY
echo "ok: resource protection health contract"

if [[ "$verify_scope" == "core" ]]; then
  for service in worker-download worker-import worker-operations scheduler; do
    status="$(compose ps -a "$service" --format '{{.State}}')"
    if [[ -n "$status" && "$status" != "exited" && "$status" != "created" ]]; then
      echo "Core rollout requires $service to remain stopped, got: $status" >&2
      exit 1
    fi
  done
  echo "ok: background workers remain stopped for core rollout"
fi
if [[ "$verify_scope" == "import" ]]; then
  for service in worker-download scheduler; do
    status="$(compose ps -a "$service" --format '{{.State}}')"
    if [[ -n "$status" && "$status" != "exited" && "$status" != "created" ]]; then
      echo "Import/search rollout requires $service to remain stopped, got: $status" >&2
      exit 1
    fi
  done
  echo "ok: download and scheduler remain stopped for import/search rollout"
fi

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

compose exec -T admin-web wget -q -O /dev/null http://127.0.0.1:3000/admin/login
echo "ok: admin-web login page"

head_revision="$(compose exec -T backend alembic heads | awk '{print $1}' | tail -n 1)"
current_revision="$(compose exec -T backend alembic current | awk '{print $1}' | tail -n 1)"
if [[ -z "$head_revision" || "$current_revision" != "$head_revision" ]]; then
  echo "Alembic revision mismatch: current=${current_revision:-none} head=${head_revision:-none}" >&2
  exit 1
fi
echo "ok: alembic current $current_revision"

compose exec -T -e REDISCLI_AUTH="${REDIS_PASSWORD:?REDIS_PASSWORD is required}" redis redis-cli ping >/dev/null
echo "ok: redis ping"

if [[ "$verify_scope" == "full" ]]; then
  current_container_hostname() {
    local service="$1" container_id hostname
    container_id="$(compose ps -q "$service")"
    if [[ -z "$container_id" ]]; then
      echo "Cannot resolve current container for $service" >&2
      return 1
    fi
    hostname="$(docker inspect "$container_id" --format '{{.Config.Hostname}}')"
    if [[ ! "$hostname" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
      echo "Current container for $service has an invalid hostname" >&2
      return 1
    fi
    printf '%s\n' "$hostname"
  }

  rq_download_hostname="$(current_container_hostname worker-download)"
  rq_import_hostname="$(current_container_hostname worker-import)"
  rq_operations_hostname="$(current_container_hostname worker-operations)"
  rq_scheduler_hostname="$(current_container_hostname scheduler)"

  compose exec -T \
    -e VERIFY_RQ_DOWNLOAD_HOSTNAME="$rq_download_hostname" \
    -e VERIFY_RQ_IMPORT_HOSTNAME="$rq_import_hostname" \
    -e VERIFY_RQ_OPERATIONS_HOSTNAME="$rq_operations_hostname" \
    -e VERIFY_RQ_SCHEDULER_HOSTNAME="$rq_scheduler_hostname" \
    backend python3 - <<'PY' \
    | VERIFY_RQ_HEARTBEAT_MAX_AGE_SECONDS="${VERIFY_RQ_HEARTBEAT_MAX_AGE_SECONDS:-480}" \
      python3 scripts/verify-rq-worker-registrations.py
import json
import os
from datetime import datetime, timezone

from redis import Redis
from rq import Worker

connection = Redis.from_url(os.environ["REDIS_URL"])
workers = []
for worker in Worker.all(connection=connection):
    last_heartbeat = getattr(worker, "last_heartbeat", None)
    workers.append(
        {
            "hostname": getattr(worker, "hostname", None),
            "queues": list(worker.queue_names()),
            "last_heartbeat": (
                last_heartbeat.isoformat() if last_heartbeat is not None else None
            ),
        }
    )
print(
    json.dumps(
        {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "expected_hostnames": {
                "download": os.environ["VERIFY_RQ_DOWNLOAD_HOSTNAME"],
                "import": os.environ["VERIFY_RQ_IMPORT_HOSTNAME"],
                "operations": os.environ["VERIFY_RQ_OPERATIONS_HOSTNAME"],
                "scheduler": os.environ["VERIFY_RQ_SCHEDULER_HOSTNAME"],
            },
            "workers": workers,
        },
        separators=(",", ":"),
    )
)
PY
  echo "ok: fresh RQ registrations cover all service-owned queues"
fi

compose exec -T backend python3 - <<'PY'
import os
from redis import Redis
from rq import Queue

connection = Redis.from_url(os.environ["REDIS_URL"])
for name in (
    "default",
    "downloads",
    "downloads:pixiv",
    "downloads:danbooru",
    "downloads:iwara",
    "downloads:weibo",
    "downloads:bilibili",
    "downloads:pinterest",
    "downloads:lofter",
    "downloads:x",
    "imports",
    "maintenance",
    "operations",
    "discovery",
    "scheduled",
):
    queue = Queue(name, connection=connection)
    len(queue)
    queue.scheduled_job_registry.count
print("ok: rq queues readable")
PY

echo "Runtime verification complete."
