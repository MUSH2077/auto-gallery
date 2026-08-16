#!/usr/bin/env bash
set -Eeuo pipefail

root="${1:?test root required}"
backend_url="${2:?backend URL required}"
admin_url="${3:?admin URL required}"
report_root="${4:?report root required}"
duration="${COEXISTENCE_DURATION_SECONDS:-7200}"
deadline=$(( $(date +%s) + duration ))

while [[ "$(date +%s)" -lt "$deadline" ]]; do
  curl -fsS --max-time 10 -o "$report_root/health.latest.json" \
    -w '{"probe":"backend","seconds":%{time_total}}\n' \
    "$backend_url/api/v1/system/health" >>"$report_root/latency.jsonl"
  python3 - "$report_root/health.latest.json" "$report_root/health.jsonl" <<'PY'
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
with open(sys.argv[2], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
PY
  curl -fsS --max-time 10 -o /dev/null \
    -w '{"probe":"admin","seconds":%{time_total}}\n' "$admin_url/" >>"$report_root/latency.jsonl"
  curl -kfsS --max-time 10 -o /dev/null \
    -w '{"probe":"nas","seconds":%{time_total}}\n' https://127.0.0.1/ >>"$report_root/latency.jsonl"
  python3 scripts/load/io-pressure.py --root "$root" --mode sequential \
    --rate-mib 4 --size-mib 256 --duration 60
  python3 scripts/load/io-pressure.py --root "$root" --mode random \
    --rate-mib 1 --iops 100 --size-mib 256 --duration 30
  sleep 120
done
