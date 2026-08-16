#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$PROJECT_ROOT"
umask 077

ROLLBACK_ROOT="${DEPLOY_SNAPSHOT_ROOT:-$(dirname "$PROJECT_ROOT")/auto-gallery-deployments}"
ACTIVE_STATE="$ROLLBACK_ROOT/active-deployment.env"
[[ -s "$ACTIVE_STATE" ]] || { echo "No active core deployment exists" >&2; exit 2; }
# shellcheck disable=SC1090
source "$ACTIVE_STATE"
[[ "${DEPLOYMENT_SCOPE:-}" == "core" && "${ROLLOUT_STAGE:-}" == "shadow" ]] || {
  echo "Core shadow observation is only valid before profile rollout" >&2
  exit 2
}
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

report_root="$ROLLBACK_DIR/shadow-observation"
install -d -m 700 "$report_root"
backend_port="${BACKEND_PORT:-8818}"
admin_port="${ADMIN_WEB_PORT:-13000}"
CORE_SMOKE_DURATION_SECONDS="${CORE_SHADOW_SOAK_SECONDS:-7200}" \
python3 scripts/test-guardian.py \
  --project auto-gallery \
  --policy deploy-core \
  --report "$report_root/guardian.jsonl" \
  --baseline-file "$report_root/host-baseline.json" \
  --probe-state "$report_root/probe-state.json" \
  -- scripts/core-smoke.sh \
    "http://127.0.0.1:$backend_port" \
    "http://127.0.0.1:$admin_port" \
    "$report_root"

completed_at="$(date +%s)"
python3 - "$ACTIVE_STATE" "$completed_at" <<'PY'
from pathlib import Path
import os
import sys
import tempfile

path = Path(sys.argv[1])
completed_at = sys.argv[2]
values: dict[str, str] = {}
for line in path.read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        values[key] = value
values["CORE_SOAK_PASSED_AT_EPOCH"] = completed_at
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

echo "Core shadow coexistence observation passed at $completed_at"
