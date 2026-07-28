#!/usr/bin/env bash
# PreToolUse(Edit|Write|MultiEdit): block edits to secret-bearing files.
# Enforces .claude/constraints/security.md deterministically (not prompt-only).
# Reads the hook payload as JSON on stdin; exit 2 blocks the tool call.
set -euo pipefail

input="$(cat)"
fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')"
[ -z "$fp" ] && exit 0

base="$(basename "$fp")"

# Allow committed env *templates* — these carry no real secrets.
case "$base" in
  .env.example|.env.ci|.env.sample|.env.template) exit 0 ;;
esac

# Block real secret stores: .env / .env.* , gallery-dl cookies, and the
# on-disk secret_key / bootstrap_admin_password material.
if [ "$base" = ".env" ] \
   || printf '%s' "$fp" | grep -qE '(^|/)\.env(\.[A-Za-z0-9_-]+)?$' \
   || printf '%s' "$fp" | grep -qE '/cookies/' \
   || printf '%s' "$fp" | grep -qE '(secret_key|bootstrap_admin_password)$'; then
  echo "BLOCKED: '$fp' holds secrets/credentials. Per .claude/constraints/security.md the agent must never edit .env files, gallery-dl cookies, or secret_key/bootstrap_admin_password. If a change is truly required, edit it on the host directly." >&2
  exit 2
fi

exit 0
