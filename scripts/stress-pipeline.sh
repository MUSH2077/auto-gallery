#!/usr/bin/env bash
# Compatibility wrapper for the isolated acceptance harness. This command no
# longer reads production .env, uses latest tags, or creates its own Compose
# state. Build and seed an immutable acceptance run first.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$PROJECT_ROOT"

STATE_ROOT="${TEST_STATE_ROOT:-$(dirname "$PROJECT_ROOT")/auto-gallery-test-runs}"
[[ -s "$STATE_ROOT/current" ]] || {
  echo "No isolated acceptance run exists; run scripts/test-env.sh build first" >&2
  exit 2
}

if [[ "${STRESS_DURATION_SECONDS:-}" != "" ]]; then
  export COEXISTENCE_DURATION_SECONDS="$STRESS_DURATION_SECONDS"
fi

exec scripts/run-acceptance.sh coexistence
