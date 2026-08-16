#!/usr/bin/env bash
set -Eeuo pipefail

backend_url="${1:?backend URL required}"
admin_url="${2:?admin URL required}"
report_root="${3:?report root required}"
duration="${CORE_SMOKE_DURATION_SECONDS:-1800}"
interval="${CORE_SMOKE_INTERVAL_SECONDS:-10}"
deadline=$(( $(date +%s) + duration ))
latency_file="$report_root/core-smoke-latency.jsonl"
probe_state="$report_root/probe-state.json"
install -d -m 700 "$report_root"
: >"$latency_file"

while [[ "$(date +%s)" -lt "$deadline" ]]; do
  nas_failures=0
  curl -fsS --max-time 10 -o "$report_root/core-health.latest.json" \
    -w '{"probe":"backend","seconds":%{time_total}}\n' \
    "$backend_url/api/v1/system/health" >>"$latency_file"
  curl -fsS --max-time 10 -o /dev/null \
    -w '{"probe":"admin","seconds":%{time_total}}\n' \
    "$admin_url/" >>"$latency_file"
  if ! curl -kfsS --max-time 10 -o /dev/null \
    -w '{"probe":"nas","seconds":%{time_total}}\n' \
    https://127.0.0.1/ >>"$latency_file"; then
    nas_failures=1
  fi
  python3 - "$latency_file" "$probe_state" "$nas_failures" <<'PY'
import json
import math
import os
from pathlib import Path
import sys

source, target, current_failures = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
rows = [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
foreground = [float(row["seconds"]) for row in rows if row.get("probe") in {"backend", "admin"}]
ordered = sorted(foreground[-120:])
p95 = ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] if ordered else 0.0
previous_failures = 0
try:
    previous_failures = int(json.loads(target.read_text()).get("nas_probe_failures") or 0)
except (OSError, json.JSONDecodeError):
    pass
payload = {
    "foreground_p95_seconds": p95,
    "nas_probe_failures": previous_failures + current_failures if current_failures else 0,
}
temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(payload) + "\n")
os.chmod(temporary, 0o600)
os.replace(temporary, target)
PY
  sleep "$interval"
done
