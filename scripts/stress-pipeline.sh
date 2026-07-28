#!/usr/bin/env bash
# Isolated pipeline stress harness. Uses a separate compose project and data root.
set -euo pipefail
cd "$(dirname "$0")/.."

env_file="${COMPOSE_ENV_FILE:-.env}"
if [[ -f "$env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$env_file"
  set +a
fi

stress_project="${STRESS_PROJECT:-auto-gallery-stress}"
stress_root="${STRESS_ROOT:-$(mktemp -d /tmp/auto-gallery-stress.XXXXXX)}"
duration="${STRESS_DURATION_SECONDS:-7200}"
poll_seconds="${STRESS_POLL_SECONDS:-60}"
stale_after="${STUCK_AFTER_SECONDS:-300}"

export COMPOSE_PROJECT_NAME="$stress_project"
export HOST_POSTGRES="$stress_root/postgres"
export HOST_REDIS="$stress_root/redis"
export HOST_MEILISEARCH="$stress_root/meilisearch"
export HOST_DOWNLOADS="$stress_root/downloads"
export HOST_LIBRARY="$stress_root/library"
export HOST_CONFIG_GALLERYDL="$stress_root/gallery-dl"
export HOST_CONFIG_APP="$stress_root/app-config"
export BACKEND_PORT="${STRESS_BACKEND_PORT:-18818}"
export ADMIN_WEB_PORT="${STRESS_ADMIN_WEB_PORT:-13080}"

# Provide safe non-placeholder defaults when the caller runs without .env.
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-stress-postgres-password}"
export REDIS_PASSWORD="${REDIS_PASSWORD:-stress-redis-password}"
export MEILI_MASTER_KEY="${MEILI_MASTER_KEY:-stress-meili-master-key}"
export SECRET_KEY="${SECRET_KEY:-stress-secret-key-change-me-not}"
export ADMIN_PASSWORD="${ADMIN_PASSWORD:-stress-admin-password}"

mkdir -p "$HOST_POSTGRES" "$HOST_REDIS" "$HOST_MEILISEARCH" "$HOST_DOWNLOADS" "$HOST_LIBRARY" "$HOST_CONFIG_GALLERYDL" "$HOST_CONFIG_APP"

compose() {
  if [[ -f "$env_file" ]]; then
    docker compose --env-file "$env_file" "$@"
  else
    docker compose "$@"
  fi
}

cleanup() {
  if [[ "${STRESS_KEEP:-0}" != "1" ]]; then
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    docker run --rm -v "$stress_root:/stress-root" auto-gallery-backend:latest \
      sh -c 'rm -rf /stress-root/* /stress-root/.[!.]* /stress-root/..?*' >/dev/null 2>&1 || true
    rmdir "$stress_root" >/dev/null 2>&1 || true
  else
    echo "Keeping stress stack '$stress_project' and data root: $stress_root"
  fi
}
trap cleanup EXIT

echo "== Auto Gallery isolated pipeline stress =="
echo "project=$stress_project"
echo "data_root=$stress_root"
echo "duration_seconds=$duration"

compose up -d postgres redis meilisearch backend worker-download worker-import worker-operations scheduler admin-web

echo "Waiting for runtime readiness..."
ready=0
for _ in $(seq 1 30); do
  if ./scripts/verify-runtime.sh; then
    ready=1
    break
  fi
  sleep 5
done
if [[ "$ready" != "1" ]]; then
  echo "Stress runtime did not become healthy in time" >&2
  exit 1
fi

echo ""
echo "Running focused pipeline tests inside isolated backend container..."
compose run --rm -T -v "$(pwd)/backend:/app" backend python -m pytest \
  tests/test_import_file_list.py \
  tests/test_disk_import.py \
  tests/test_pipeline_health.py \
  tests/test_download_outcome.py \
  tests/test_import_outcome.py \
  tests/test_job_state_manifest.py \
  tests/test_queue_routing_scheduler.py \
  tests/test_scheduler_contract.py \
  tests/test_creator_reconcile.py \
  -q

echo ""
echo "Initial stuck check..."
COMPOSE_PROJECT_NAME="$stress_project" STUCK_AFTER_SECONDS="$stale_after" ./scripts/check-pipeline-stuck.sh

if [[ "$duration" -le 0 ]]; then
  echo "Skipping soak loop because STRESS_DURATION_SECONDS=$duration"
  exit 0
fi

deadline=$(( $(date +%s) + duration ))
iteration=0
while [[ "$(date +%s)" -lt "$deadline" ]]; do
  iteration=$((iteration + 1))
  echo ""
  echo "Soak iteration $iteration at $(date -Iseconds)"
  COMPOSE_PROJECT_NAME="$stress_project" STUCK_AFTER_SECONDS="$stale_after" ./scripts/check-pipeline-stuck.sh
  compose logs --tail=200 backend worker-download worker-import worker-operations scheduler \
    | grep -E "Traceback|ERROR|CRITICAL|stuck|timeout|stalled" && {
        echo "Stress run saw error-level log output" >&2
        exit 1
      } || true
  sleep "$poll_seconds"
done

echo "Stress pipeline run completed without stuck pipeline findings."
