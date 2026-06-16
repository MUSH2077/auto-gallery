#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/generate-env.sh [--force]

Generate a local .env from .env.example with service secrets filled in.
By default, existing .env is not overwritten.
EOF
}

force=false
case "${1:-}" in
  "")
    ;;
  --force)
    force=true
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
template="$repo_root/.env.example"
target="$repo_root/.env"

if [[ ! -f "$template" ]]; then
  echo "Missing template: $template" >&2
  exit 1
fi

if [[ -e "$target" && "$force" != true ]]; then
  echo ".env already exists. Re-run with --force to replace it." >&2
  exit 1
fi

random_hex() {
  local bytes="$1"
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$bytes"
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$bytes" <<'PY'
import secrets
import sys

print(secrets.token_hex(int(sys.argv[1])))
PY
  else
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$((bytes * 2))"
    echo
  fi
}

postgres_password="ag_pg_$(random_hex 18)"
redis_password="ag_redis_$(random_hex 18)"
meili_master_key="ag_meili_$(random_hex 24)"
secret_key="$(random_hex 32)"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

awk \
  -v postgres_password="$postgres_password" \
  -v redis_password="$redis_password" \
  -v meili_master_key="$meili_master_key" \
  -v secret_key="$secret_key" \
  '
  /^POSTGRES_PASSWORD=/ { print "POSTGRES_PASSWORD=" postgres_password; next }
  /^REDIS_PASSWORD=/ { print "REDIS_PASSWORD=" redis_password; next }
  /^MEILI_MASTER_KEY=/ { print "MEILI_MASTER_KEY=" meili_master_key; next }
  /^SECRET_KEY=/ { print "SECRET_KEY=" secret_key; next }
  { print }
  ' "$template" > "$tmp"

mv "$tmp" "$target"
chmod 600 "$target"
trap - EXIT

cat <<'EOF'
Generated .env with fresh service secrets.

First login:
  username: admin
  password: change-me-admin

The web UI will force a password change after first login.
Review ports, timezone, and NAS host paths before running docker compose up -d.
EOF
