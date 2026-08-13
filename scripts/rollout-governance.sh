#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$PROJECT_ROOT"
umask 077

stage="${1:-}"
case "$stage" in
  shadow) mode=shadow; profiles=; max_scale=1.0; verify_scope=core ;;
  import) mode=enforce; profiles=import_db,image_derive,video_derive; max_scale=0.10; verify_scope=import ;;
  search) mode=enforce; profiles=import_db,image_derive,video_derive,search_index; max_scale=0.10; verify_scope=import ;;
  download) mode=enforce; profiles=import_db,image_derive,video_derive,search_index,download_network; max_scale=0.10; verify_scope=full ;;
  *) echo "usage: scripts/rollout-governance.sh shadow|import|search|download" >&2; exit 2 ;;
esac

ROLLBACK_ROOT="${DEPLOY_SNAPSHOT_ROOT:-$(dirname "$PROJECT_ROOT")/auto-gallery-deployments}"
ACTIVE_STATE="$ROLLBACK_ROOT/active-deployment.env"
[[ -s "$ACTIVE_STATE" ]] || { echo "No verified active deployment state exists" >&2; exit 2; }
# shellcheck disable=SC1090
source "$ACTIVE_STATE"
current_stage="${ROLLOUT_STAGE:-shadow}"
changed_at="${STAGE_CHANGED_AT_EPOCH:-${SHADOW_STARTED_AT_EPOCH:-0}}"
now="$(date +%s)"

minimum_wait=0
case "$current_stage:$stage" in
  shadow:import) minimum_wait=$((24 * 60 * 60)) ;;
  import:search|search:download) minimum_wait=$((6 * 60 * 60)) ;;
  shadow:shadow|import:import|search:search|download:download) minimum_wait=0 ;;
  import:shadow|search:shadow|search:import|download:shadow|download:import|download:search) minimum_wait=0 ;;
  *) echo "Refusing to skip rollout stages: $current_stage -> $stage" >&2; exit 2 ;;
esac
if (( minimum_wait > 0 && now - changed_at < minimum_wait )); then
  echo "Refusing early rollout: $current_stage must remain observed for ${minimum_wait}s" >&2
  echo "Remaining: $((minimum_wait - (now - changed_at)))s" >&2
  exit 2
fi
if [[ "$current_stage:$stage" == "shadow:import" ]]; then
  soak_passed_at="${CORE_SOAK_PASSED_AT_EPOCH:-0}"
  if (( soak_passed_at < ${SHADOW_STARTED_AT_EPOCH:-0} )); then
    echo "Refusing import rollout: run scripts/observe-core-shadow.sh successfully first" >&2
    exit 2
  fi
fi

[[ -f .env ]] || { echo ".env is missing" >&2; exit 2; }
mode_bits="$(stat -c '%a' .env)"
(( (8#$mode_bits & 8#022) == 0 )) || { echo ".env is group/other writable" >&2; exit 2; }

env_backup="$(mktemp "$ROLLBACK_ROOT/.env.rollout-backup.XXXXXX")"
cp .env "$env_backup"
chmod 600 "$env_backup"
committed=0
rollout_failed() {
  local status=$?
  trap - ERR EXIT
  if [[ "$committed" -ne 1 ]]; then
    cp "$env_backup" .env
    chmod 600 .env
    docker compose stop -t 60 worker-download worker-import worker-operations scheduler >/dev/null 2>&1 || true
    docker compose up -d --no-deps --force-recreate backend admin-web >/dev/null 2>&1 || true
    echo "Rollout failed closed; previous .env restored and heavy workers stopped" >&2
  fi
  rm -f -- "$env_backup"
  exit "$status"
}
trap rollout_failed ERR EXIT

python3 - "$mode" "$profiles" "$max_scale" <<'PY'
from pathlib import Path
import os, sys, tempfile

path = Path(".env")
mode, profiles, max_scale = sys.argv[1:]
updates = {
    "RESOURCE_GOVERNANCE_MODE": mode,
    "RESOURCE_GOVERNANCE_MAX_SCALE": max_scale,
    "RESOURCE_GOVERNANCE_ENFORCED_PROFILES": profiles,
    "GITLLERY_PROJECTION_MODE": "shadow",
    "DOWNLOAD_CONCURRENCY_CAP": "1",
}
seen = set()
lines = []
for line in path.read_text(encoding="utf-8").splitlines():
    key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
    if key in updates:
        lines.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        lines.append(line)
for key, value in updates.items():
    if key not in seen:
        lines.append(f"{key}={value}")
fd, raw = tempfile.mkstemp(prefix=".env.rollout.", dir=".")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(raw, 0o600)
    os.replace(raw, path)
finally:
    if os.path.exists(raw):
        os.unlink(raw)
PY

case "$stage" in
  shadow)
    docker compose stop -t 60 worker-download worker-import worker-operations scheduler || true
    docker compose up -d --no-deps --force-recreate backend admin-web
    ;;
  import|search)
    docker compose stop -t 60 worker-download scheduler || true
    docker compose up -d --no-deps --force-recreate \
      backend admin-web worker-import worker-operations
    ;;
  download)
    docker compose up -d --no-deps --force-recreate \
      backend admin-web worker-download worker-import worker-operations scheduler
    ;;
esac

for attempt in $(seq 1 60); do
  unhealthy="$(docker compose ps --format '{{.Service}} {{.State}} {{.Health}}' \
    | awk '$2 != "running" || ($3 != "" && $3 != "healthy") {print}')"
  [[ -z "$unhealthy" ]] && break
  [[ "$attempt" -lt 60 ]] || { echo "$unhealthy" >&2; false; }
  sleep 5
done
VERIFY_EXPECT_GOVERNANCE_MODE="$mode" \
VERIFY_EXPECT_ENFORCED_PROFILES="$profiles" \
VERIFY_EXPECT_MAX_SCALE="$max_scale" \
VERIFY_REQUIRE_NORMAL_PRESSURE=0 VERIFY_SCOPE="$verify_scope" scripts/verify-runtime.sh

python3 - "$ACTIVE_STATE" "$stage" "$now" <<'PY'
from pathlib import Path
import os, sys, tempfile

path = Path(sys.argv[1])
stage, changed = sys.argv[2:]
values = {}
for line in path.read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        values[key] = value
values["ROLLOUT_STAGE"] = stage
values["STAGE_CHANGED_AT_EPOCH"] = changed
if stage == "shadow":
    values["SHADOW_STARTED_AT_EPOCH"] = changed
fd, raw = tempfile.mkstemp(prefix="active-deployment.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(raw, 0o600)
    os.replace(raw, path)
finally:
    if os.path.exists(raw):
        os.unlink(raw)
PY

committed=1
rm -f -- "$env_backup"
trap - ERR EXIT
echo "Resource governance rollout stage is now: $stage"
