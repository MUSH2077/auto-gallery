#!/usr/bin/env bash
set -euo pipefail

env_file="${COMPOSE_ENV_FILE:-.env}"
connect_timeout="${RECOVERY_CURL_CONNECT_TIMEOUT_SECONDS:-2}"
max_time="${RECOVERY_CURL_MAX_TIME_SECONDS:-4}"

if [[ ! "$connect_timeout" =~ ^[1-9][0-9]*$ || \
      ! "$max_time" =~ ^[1-9][0-9]*$ ]]; then
  echo "Recovery curl timeouts must be positive whole seconds" >&2
  exit 2
fi
if (( connect_timeout > max_time )); then
  echo "Recovery curl connect timeout cannot exceed max time" >&2
  exit 2
fi

compose_args=(docker compose)
if [[ -f "$env_file" ]]; then
  compose_args+=(--env-file "$env_file")
fi

"${compose_args[@]}" exec -T backend \
  curl --silent --show-error --fail \
    --connect-timeout "$connect_timeout" \
    --max-time "$max_time" \
    http://localhost:8000/api/v1/system/health \
  | python3 scripts/verify-resource-recovery.py
