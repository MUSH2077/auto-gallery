#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
STATE_ROOT="${TEST_STATE_ROOT:-$(dirname "$PROJECT_ROOT")/auto-gallery-test-runs}"
# shellcheck disable=SC1090
source "$STATE_ROOT/current"
# shellcheck disable=SC1090
source "$TEST_ENV"
export COMPOSE_PROJECT_NAME="$TEST_PROJECT"

compose() {
  docker compose --project-directory "$PROJECT_ROOT" -p "$TEST_PROJECT" \
    --env-file "$TEST_ENV" -f "$PROJECT_ROOT/docker-compose.yaml" \
    -f "$PROJECT_ROOT/docker-compose.test.yaml" "$@"
}

[[ -f "$TEST_ROOT/.auto-gallery-test-root" ]]
compose up -d postgres redis meilisearch migrate backend worker-operations

echo "fault: Meilisearch unavailable must degrade search without failing readiness"
compose stop -t 15 meilisearch
compose exec -T backend python - <<'PY'
import httpx
ready = httpx.get("http://localhost:8000/api/v1/system/ready", timeout=15)
assert ready.status_code == 200, ready.text
health = httpx.get("http://localhost:8000/api/v1/system/health", timeout=15)
health.raise_for_status()
assert health.json()["status"] in {"ok", "degraded"}
PY
compose start meilisearch

echo "fault: Redis noeviction must reject writes without deleting queue data"
used="$(compose exec -T redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --no-auth-warning INFO memory' | awk -F: '/used_memory:/ {gsub(/\r/,"",$2); print $2}')"
restore_redis_capacity() {
  compose exec -T redis sh -c \
    'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --no-auth-warning CONFIG SET maxmemory 100663296 >/dev/null' \
    >/dev/null 2>&1 || true
}
trap restore_redis_capacity EXIT
warn_limit=$((used * 100 / 85))
stop_limit=$((used * 100 / 90))
compose exec -T redis sh -c "REDISCLI_AUTH=\"\$REDIS_PASSWORD\" redis-cli --no-auth-warning CONFIG SET maxmemory '$warn_limit' >/dev/null"
compose run --rm --no-deps backend python - <<'PY'
from app.services.queue_admission import ensure_redis_enqueue_capacity

sample = ensure_redis_enqueue_capacity()
assert 0.80 <= float(sample["usage_ratio"]) < 0.90, sample
PY
compose exec -T redis sh -c "REDISCLI_AUTH=\"\$REDIS_PASSWORD\" redis-cli --no-auth-warning CONFIG SET maxmemory '$stop_limit' >/dev/null"
compose run --rm --no-deps backend python - <<'PY'
from app.services.queue_admission import QueueAdmissionError, ensure_redis_enqueue_capacity

try:
    ensure_redis_enqueue_capacity()
except QueueAdmissionError as exc:
    assert exc.code == "redis_capacity", exc.payload()
else:
    raise AssertionError("Redis 90% capacity gate accepted an enqueue")
PY
limit=$((used + 262144))
compose exec -T redis sh -c "REDISCLI_AUTH=\"\$REDIS_PASSWORD\" redis-cli --no-auth-warning CONFIG SET maxmemory '$limit' >/dev/null"
set +e
write_output="$(compose exec -T redis sh -c 'head -c 524288 /dev/zero | tr "\000" x | REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -x --no-auth-warning SET acceptance:overflow' 2>&1)"
write_status=$?
set -e
compose exec -T redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --no-auth-warning DEL acceptance:overflow >/dev/null; REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli --no-auth-warning CONFIG SET maxmemory 100663296 >/dev/null'
trap - EXIT
if [[ "$write_status" -eq 0 && "$write_output" != *OOM* ]]; then
  echo "Redis did not reject the bounded overflow probe: $write_output" >&2
  exit 1
fi

echo "fault: Redis loss must fail readiness, then recover"
compose stop -t 15 redis
if compose exec -T backend curl -sf http://localhost:8000/api/v1/system/ready >/dev/null; then
  echo "backend readiness remained green while Redis was stopped" >&2
  exit 1
fi
compose start redis
for _ in $(seq 1 30); do
  compose exec -T backend curl -sf http://localhost:8000/api/v1/system/ready >/dev/null && break
  sleep 2
done
compose exec -T backend curl -sf http://localhost:8000/api/v1/system/ready >/dev/null

echo "fault: deterministic lifecycle/CAS cases"
compose run --rm --no-deps backend python -m pytest -q \
  tests/test_download_process_lifecycle.py \
  tests/test_download_staging.py \
  tests/test_media_derivatives.py \
  tests/test_outbox_successor_contract.py \
  tests/test_resource_aware_worker.py \
  tests/test_worker_concurrency.py \
  tests/test_worker_health_probe.py

touch "$TEST_ROOT/fault.pass"
