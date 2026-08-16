#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$PROJECT_ROOT"
umask 077

STATE_ROOT="${TEST_STATE_ROOT:-$(dirname "$PROJECT_ROOT")/auto-gallery-test-runs}"
CURRENT_FILE="$STATE_ROOT/current"
BASE_COMPOSE="$PROJECT_ROOT/docker-compose.yaml"
TEST_COMPOSE="$PROJECT_ROOT/docker-compose.test.yaml"

existing_source_paths() {
  git ls-files -co --exclude-standard -z \
    | while IFS= read -r -d '' path; do
        if [[ -f "$path" || -L "$path" ]]; then
          printf '%s\0' "$path"
        fi
      done
}

source_digest() {
  existing_source_paths \
    | sort -z \
    | while IFS= read -r -d '' path; do
        printf '%s\0' "$path"
        if [[ -L "$path" ]]; then
          printf 'symlink\0%s\n' "$(readlink -- "$path")"
        else
          sha256sum -- "$path"
        fi
      done \
    | sha256sum | awk '{print $1}'
}

random_secret() {
  python3 -c 'import secrets; print(secrets.token_hex(24))'
}

random_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

load_state() {
  [[ -s "$CURRENT_FILE" ]] || { echo "No acceptance run exists; run: scripts/test-env.sh build" >&2; exit 2; }
  # shellcheck disable=SC1090
  source "$CURRENT_FILE"
  [[ -f "$TEST_ROOT/.auto-gallery-test-root" ]] || { echo "Invalid acceptance root: $TEST_ROOT" >&2; exit 2; }
  [[ "$(cat "$TEST_ROOT/.auto-gallery-test-root")" == "$TEST_RUN_ID" ]] || { echo "Acceptance marker mismatch" >&2; exit 2; }
  export TEST_RUN_ID TEST_ROOT TEST_ENV TEST_PROJECT SOURCE_DIGEST
  export COMPOSE_PROJECT_NAME="$TEST_PROJECT"
  export COMPOSE_FILE="$BASE_COMPOSE:$TEST_COMPOSE"
}

compose() {
  docker compose --project-directory "$PROJECT_ROOT" -p "$TEST_PROJECT" \
    --env-file "$TEST_ENV" -f "$BASE_COMPOSE" -f "$TEST_COMPOSE" "$@"
}

guardian_args() {
  printf '%s\n' --project "$TEST_PROJECT" --report "$TEST_ROOT/reports/guardian.jsonl"
  printf '%s\n' --policy build-functional
  printf '%s\n' --baseline-file "$TEST_ROOT/reports/host-baseline.json"
  printf '%s\n' --probe-state "$TEST_ROOT/reports/probe-state.json"
}

guarded() {
  local -a args=()
  while IFS= read -r value; do args+=("$value"); done < <(guardian_args)
  python3 scripts/test-guardian.py "${args[@]}" -- "$@"
}

guarded_logged() {
  local label="${1:?log label required}"
  shift
  guarded "$@" 2>&1 | tee "$TEST_ROOT/reports/$label.log"
}

new_run() {
  install -d -m 700 "$STATE_ROOT"
  SOURCE_DIGEST="$(source_digest)"
  TEST_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-${SOURCE_DIGEST:0:12}"
  TEST_PROJECT="auto-gallery-test-${SOURCE_DIGEST:0:8}-$(date +%s)"
  TEST_ROOT="$STATE_ROOT/$TEST_RUN_ID"
  TEST_ENV="$TEST_ROOT/acceptance.env"
  install -d -m 700 "$TEST_ROOT" "$TEST_ROOT/reports" "$TEST_ROOT/postgres" \
    "$TEST_ROOT/redis" "$TEST_ROOT/meilisearch" "$TEST_ROOT/downloads" \
    "$TEST_ROOT/library" "$TEST_ROOT/gallery-dl" "$TEST_ROOT/app-config" \
    "$TEST_ROOT/fixtures"
  printf '%s\n' "$TEST_RUN_ID" >"$TEST_ROOT/.auto-gallery-test-root"
  printf '%s\n' "$SOURCE_DIGEST" >"$TEST_ROOT/source-digest.txt"
  local backend_port admin_port
  backend_port="$(random_port)"
  admin_port="$(random_port)"
  cat >"$TEST_ENV" <<EOF
COMPOSE_PROJECT_NAME=$TEST_PROJECT
TEST_RUN_ID=$TEST_RUN_ID
TEST_ROOT=$TEST_ROOT
TEST_FIXTURES=$TEST_ROOT/fixtures
BACKEND_IMAGE=auto-gallery-backend:candidate-$SOURCE_DIGEST
ADMIN_IMAGE=auto-gallery-admin-web:candidate-$SOURCE_DIGEST
POSTGRES_DB=autogallery
POSTGRES_USER=autogallery
POSTGRES_PASSWORD=$(random_secret)
REDIS_PASSWORD=$(random_secret)
MEILI_MASTER_KEY=$(random_secret)
SECRET_KEY=$(random_secret)$(random_secret)
ADMIN_PASSWORD=acceptance-only-password
HOST_POSTGRES=$TEST_ROOT/postgres
HOST_REDIS=$TEST_ROOT/redis
HOST_MEILISEARCH=$TEST_ROOT/meilisearch
HOST_DOWNLOADS=$TEST_ROOT/downloads
HOST_LIBRARY=$TEST_ROOT/library
HOST_CONFIG_GALLERYDL=$TEST_ROOT/gallery-dl
HOST_CONFIG_APP=$TEST_ROOT/app-config
BACKEND_PORT=$backend_port
ADMIN_WEB_PORT=$admin_port
BACKEND_INTERNAL_URL=http://backend:8000
CORS_ORIGINS=http://127.0.0.1:$admin_port
TIMEZONE=Asia/Shanghai
DOWNLOAD_CONCURRENCY_CAP=1
DOWNLOAD_QUEUE_MAX_PENDING=100
DOWNLOAD_STAGING_ENABLED=1
RESOURCE_GOVERNANCE_MODE=shadow
RESOURCE_GOVERNANCE_MAX_SCALE=1.0
RESOURCE_GOVERNANCE_ENFORCED_PROFILES=
GITLLERY_PROJECTION_MODE=shadow
GITLLERY_BUILD_GENERATION=segment-r1
EOF
  chmod 600 "$TEST_ENV"
  cat >"$CURRENT_FILE" <<EOF
TEST_RUN_ID=$TEST_RUN_ID
TEST_PROJECT=$TEST_PROJECT
TEST_ROOT=$TEST_ROOT
TEST_ENV=$TEST_ENV
SOURCE_DIGEST=$SOURCE_DIGEST
EOF
  chmod 600 "$CURRENT_FILE"
  load_state
}

build_candidate() {
  for service in worker-download worker-import worker-operations scheduler; do
    if [[ "$(docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery ps "$service" --format '{{.State}}' 2>/dev/null || true)" == "running" ]]; then
      echo "Refusing to learn an idle acceptance baseline while production $service is running" >&2
      exit 2
    fi
  done
  new_run
  local backend_base="${BACKEND_CANDIDATE_BASE_IMAGE:-auto-gallery-backend:latest}"
  docker image inspect "$backend_base" --format '{{.Id}}' >"$TEST_ROOT/backend-base-image-id.txt"
  echo "Building immutable backend candidate $SOURCE_DIGEST"
  guarded_logged backend-build scripts/build-thin-backend-candidate.sh \
    "$backend_base" "auto-gallery-backend:candidate-$SOURCE_DIGEST" \
    "$PROJECT_ROOT/backend" "$SOURCE_DIGEST" "$TEST_PROJECT"
  echo "Running frontend build and static checks in a no-swap bounded builder"
  guarded_logged frontend-build scripts/build-thin-admin-candidate.sh \
    "$PROJECT_ROOT/admin-web" "$SOURCE_DIGEST" "$TEST_PROJECT" \
    "auto-gallery-admin-web:candidate-$SOURCE_DIGEST"
  docker image inspect 'mcr.microsoft.com/playwright:v1.62.1-noble' >/dev/null 2>&1 || \
    docker pull 'mcr.microsoft.com/playwright:v1.62.1-noble'
  docker image inspect 'mcr.microsoft.com/playwright:v1.62.1-noble' \
    --format '{{.Id}}' >"$TEST_ROOT/playwright-image-id.txt"
  docker image inspect "auto-gallery-backend:candidate-$SOURCE_DIGEST" \
    --format '{{.Id}}' >"$TEST_ROOT/backend-image-id.txt"
  docker image inspect "auto-gallery-admin-web:candidate-$SOURCE_DIGEST" \
    --format '{{.Id}}' >"$TEST_ROOT/admin-image-id.txt"
  touch "$TEST_ROOT/build.pass"
  echo "Acceptance run: $TEST_RUN_ID"
}

wait_postgres() {
  local attempt
  for attempt in $(seq 1 60); do
    if compose exec -T postgres pg_isready -U autogallery >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

seed_snapshot_inner() {
  load_state
  [[ -f "$TEST_ROOT/build.pass" ]] || { echo "Candidate has not been built" >&2; exit 2; }
  local raw_dump="$TEST_ROOT/production.raw.dump"
  cleanup_raw_dump() {
    rm -f -- "$raw_dump"
  }
  trap cleanup_raw_dump EXIT
  compose up -d postgres
  wait_postgres
  echo "Capturing a protected logical production snapshot"
  docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery exec -T postgres sh -c \
    'exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom --compress=3' \
    >"$raw_dump"
  [[ -s "$raw_dump" ]]
  docker compose --project-directory "$PROJECT_ROOT" -p auto-gallery exec -T postgres sh -c \
    'psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --tuples-only --no-align --command="SELECT version_num FROM alembic_version"' \
    | tr -d '[:space:]' >"$TEST_ROOT/predeploy-revision.txt"
  compose exec -T postgres pg_restore --clean --if-exists --no-owner --no-privileges \
    -U autogallery -d autogallery <"$raw_dump"
  compose run --rm --no-deps migrate alembic upgrade head
  compose run --rm --no-deps backend python scripts/sanitize_test_snapshot.py
  compose run --rm --no-deps backend python scripts/augment_acceptance_scale.py
  compose run --rm --no-deps -v "$TEST_ROOT:/acceptance" backend \
    python scripts/generate_acceptance_media.py --root /acceptance/fixtures --works 200
  compose exec -T postgres psql -U autogallery -d autogallery -At -F, -c \
    "SELECT (SELECT count(*) FROM works), (SELECT count(*) FROM assets), (SELECT count(*) FROM work_tags) + (SELECT count(*) FROM work_source_tags), (SELECT count(*) FROM storage_artifacts)" \
    >"$TEST_ROOT/scale.csv"
  IFS=, read -r works assets tag_relations artifacts <"$TEST_ROOT/scale.csv"
  [[ "$works" -ge 67000 && "$assets" -ge 90000 && "$tag_relations" -ge 470000 && "$artifacts" -ge 330000 ]] || {
    echo "Restored fixture is below the acceptance scale floor: $(cat "$TEST_ROOT/scale.csv")" >&2
    exit 1
  }
  local predeploy head
  predeploy="$(cat "$TEST_ROOT/predeploy-revision.txt")"
  head="$(compose run --rm --no-deps migrate alembic heads | awk '{print $1}' | tail -1)"
  [[ "$predeploy" =~ ^[a-f0-9]{12}$ && "$head" =~ ^[a-f0-9]{12}$ ]]
  if [[ "$predeploy" != "$head" ]]; then
    compose run --rm --no-deps migrate alembic downgrade "$predeploy"
    compose run --rm --no-deps migrate alembic upgrade "$head"
  fi
  compose exec -T postgres pg_dump -U autogallery -d autogallery --schema-only \
    | sha256sum >"$TEST_ROOT/sanitized-schema.sha256"
  cleanup_raw_dump
  trap - EXIT
  touch "$TEST_ROOT/seed.pass"
}

seed_snapshot() {
  load_state
  guarded_logged seed "$PROJECT_ROOT/scripts/test-env.sh" _seed
}

run_tests_inner() {
  load_state
  [[ -f "$TEST_ROOT/seed.pass" ]] || { echo "Acceptance data has not been seeded" >&2; exit 2; }
  compose up -d postgres redis meilisearch migrate
  compose run --rm --no-deps backend ruff check app tests scripts worker_entrypoint.py seed_sync.py
  python3 scripts/check-governance-contracts.py
  compose run --rm --no-deps backend python -m pytest -q
  compose run --rm --no-deps backend python scripts/export_api_contracts.py --check
  compose up -d backend worker-download worker-import worker-operations scheduler admin-web provider-stub
  local attempt
  for attempt in $(seq 1 60); do
    if VERIFY_REQUIRE_NORMAL_PRESSURE=0 COMPOSE_ENV_FILE="$TEST_ENV" COMPOSE_PROJECT_NAME="$TEST_PROJECT" \
      COMPOSE_FILE="$BASE_COMPOSE:$TEST_COMPOSE" scripts/verify-runtime.sh; then
      touch "$TEST_ROOT/runtime.pass"
      break
    fi
    sleep 5
  done
  [[ -f "$TEST_ROOT/runtime.pass" ]]
  lock_digest="$(sha256sum admin-web/package.json admin-web/package-lock.json | sha256sum | awk '{print $1}')"
  dependency_cache="/var/tmp/auto-gallery-admin-build-root/deps-${lock_digest:0:16}/node_modules"
  [[ -d "$dependency_cache" ]]
  docker run --rm --network "${TEST_PROJECT}_default" \
    --memory 640m --memory-swap 640m --cpus 0.35 --pids-limit 128 \
    --label "com.docker.compose.project=$TEST_PROJECT" \
    --label 'com.auto-gallery.environment=acceptance' \
    --mount "type=bind,src=$PROJECT_ROOT/admin-web,dst=/app,readonly" \
    --mount "type=bind,src=$dependency_cache,dst=/app/node_modules,readonly" \
    --workdir /app \
    -e PLAYWRIGHT_BASE_URL=http://admin-web:3000 \
    'mcr.microsoft.com/playwright:v1.62.1-noble' \
    npx playwright test --project=chromium --workers=1
  compose stop -t 60 worker-download worker-import worker-operations scheduler
  backend_port="$(awk -F= '$1=="BACKEND_PORT" {print $2}' "$TEST_ENV")"
  admin_port="$(awk -F= '$1=="ADMIN_WEB_PORT" {print $2}' "$TEST_ENV")"
  CORE_SMOKE_DURATION_SECONDS="${CORE_SMOKE_DURATION_SECONDS:-1800}" \
    scripts/core-smoke.sh \
      "http://127.0.0.1:$backend_port" \
      "http://127.0.0.1:$admin_port" \
      "$TEST_ROOT/reports"
  touch "$TEST_ROOT/core-smoke.pass"
  touch "$TEST_ROOT/functional.pass"
}

run_tests() {
  load_state
  guarded_logged functional "$PROJECT_ROOT/scripts/test-env.sh" _test
}

report_run() {
  load_state
  python3 scripts/acceptance-report.py --root "$TEST_ROOT" --project "$TEST_PROJECT" \
    --scope "${ACCEPTANCE_SCOPE:-full}"
}

down_run() {
  load_state
  compose down --volumes --remove-orphans
  echo "Stopped isolated project $TEST_PROJECT; reports remain at $TEST_ROOT"
}

case "${1:-}" in
  build) build_candidate ;;
  seed) seed_snapshot ;;
  test) run_tests ;;
  _seed) seed_snapshot_inner ;;
  _test) run_tests_inner ;;
  soak) load_state; scripts/run-acceptance.sh coexistence ;;
  report) report_run ;;
  down) down_run ;;
  *) echo "usage: scripts/test-env.sh build|seed|test|soak|report|down" >&2; exit 2 ;;
esac
