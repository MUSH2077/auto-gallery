#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$PROJECT_ROOT"
STATE_ROOT="${TEST_STATE_ROOT:-$(dirname "$PROJECT_ROOT")/auto-gallery-test-runs}"
requested_action="${1:-}"
report_scope=full
[[ "$requested_action" == "core" ]] && report_scope=core

acceptance_failed() {
  local status=$?
  trap - ERR
  if [[ -s "$STATE_ROOT/current" ]]; then
    # shellcheck disable=SC1090
    source "$STATE_ROOT/current"
    if [[ -f "${TEST_ROOT:-}/.auto-gallery-test-root" && -f "${TEST_ROOT:-}/source-digest.txt" ]]; then
      python3 scripts/acceptance-report.py --root "$TEST_ROOT" --project "$TEST_PROJECT" \
        --scope "$report_scope" >"$TEST_ROOT/reports/failure-manifest.log" 2>&1 || true
    fi
  fi
  exit "$status"
}
trap acceptance_failed ERR

load_state() {
  # shellcheck disable=SC1090
  source "$STATE_ROOT/current"
  # shellcheck disable=SC1090
  source "$TEST_ENV"
  export TEST_RUN_ID TEST_PROJECT TEST_ROOT TEST_ENV SOURCE_DIGEST
  export COMPOSE_PROJECT_NAME="$TEST_PROJECT"
}

compose() {
  docker compose --project-directory "$PROJECT_ROOT" -p "$TEST_PROJECT" \
    --env-file "$TEST_ENV" -f docker-compose.yaml -f docker-compose.test.yaml "$@"
}

guardian() {
  local phase="$1"
  local policy="$2"
  shift 2
  python3 scripts/test-guardian.py --project "$TEST_PROJECT" \
    --report "$TEST_ROOT/reports/${phase}-guardian.jsonl" \
    --policy "$policy" \
    --baseline-file "$TEST_ROOT/reports/host-baseline.json" \
    --probe-state "$TEST_ROOT/reports/probe-state.json" -- "$@"
}

functional() {
  if [[ ! -s "$STATE_ROOT/current" ]]; then scripts/test-env.sh build; fi
  load_state
  [[ -f "$TEST_ROOT/build.pass" ]] || scripts/test-env.sh build
  load_state
  [[ -f "$TEST_ROOT/seed.pass" ]] || scripts/test-env.sh seed
  [[ -f "$TEST_ROOT/functional.pass" ]] || scripts/test-env.sh test
}

fault() {
  load_state
  [[ -f "$TEST_ROOT/functional.pass" ]]
  guardian fault load-soak scripts/fault-matrix.sh
}

performance() {
  load_state
  [[ -f "$TEST_ROOT/functional.pass" ]]
  compose up -d postgres redis meilisearch migrate backend worker-import worker-operations
  guardian performance load-soak docker compose --project-directory "$PROJECT_ROOT" -p "$TEST_PROJECT" \
    --env-file "$TEST_ENV" -f docker-compose.yaml -f docker-compose.test.yaml \
    run --rm --no-deps backend python scripts/benchmark_work_search.py \
    --require-scale --target-ms 500 --sql-budget 4 --count-concurrency 20
  install -d -m 700 "$TEST_ROOT/pipeline-scaling"
  guardian performance load-soak docker compose --project-directory "$PROJECT_ROOT" -p "$TEST_PROJECT" \
    --env-file "$TEST_ENV" -f docker-compose.yaml -f docker-compose.test.yaml \
    run --rm --no-deps -v "$TEST_ROOT:/acceptance" backend \
    python scripts/benchmark_discovery_scaling.py \
    --root /acceptance/pipeline-scaling --works 200 --existing-files 100000 \
    --create-iops 250 --max-growth 1.20 \
    --output /acceptance/reports/discovery-scaling.json
  compose run --rm --no-deps backend python scripts/exercise_resource_controller.py \
    >"$TEST_ROOT/reports/controller-scenarios.json"
  guardian performance load-soak python3 scripts/load/io-pressure.py --root "$TEST_ROOT" --mode fsync \
    --rate-mib 8 --size-mib 512 --duration 120
  guardian performance load-soak docker compose --project-directory "$PROJECT_ROOT" -p "$TEST_PROJECT" \
    --env-file "$TEST_ENV" -f docker-compose.yaml -f docker-compose.test.yaml \
    --profile load run --rm pressure-memory
  touch "$TEST_ROOT/performance.pass"
}

coexistence() {
  load_state
  [[ -f "$TEST_ROOT/performance.pass" ]]
  compose up -d
  compose stop -t 60 worker-operations
  compose run --rm --no-deps backend python scripts/seed_acceptance_backlog.py --limit 5000
  compose start worker-operations
  backend_port="$(awk -F= '$1=="BACKEND_PORT" {print $2}' "$TEST_ENV")"
  admin_port="$(awk -F= '$1=="ADMIN_WEB_PORT" {print $2}' "$TEST_ENV")"
  guardian coexistence load-soak scripts/coexistence-workload.sh "$TEST_ROOT" \
    "http://127.0.0.1:$backend_port" "http://127.0.0.1:$admin_port" \
    "$TEST_ROOT/reports"
  python3 scripts/validate-coexistence.py --root "$TEST_ROOT" \
    --expected-duration "${COEXISTENCE_DURATION_SECONDS:-7200}"
  touch "$TEST_ROOT/coexistence.pass"
}

core() {
  functional
  fault
  ACCEPTANCE_SCOPE=core scripts/test-env.sh report
}

case "${1:-}" in
  functional) functional ;;
  fault) fault ;;
  performance) performance ;;
  coexistence) coexistence ;;
  core) core ;;
  full)
    functional
    fault
    performance
    coexistence
    scripts/test-env.sh report
    ;;
  *) echo "usage: scripts/run-acceptance.sh functional|fault|performance|coexistence|core|full" >&2; exit 2 ;;
esac

trap - ERR
