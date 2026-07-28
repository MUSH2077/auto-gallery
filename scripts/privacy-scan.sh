#!/usr/bin/env bash
set -euo pipefail

fail=0

tracked_files="$(mktemp)"
trap 'rm -f "$tracked_files"' EXIT
git ls-files -z > "$tracked_files"

local_user="${PRIVACY_SCAN_LOCAL_USER:-${USER:-}}"
# The official repository owner is public project metadata. Set this variable
# only when a separate, private namespace must be rejected during a release.
forbidden_namespace="${PRIVACY_SCAN_FORBIDDEN_NAMESPACE:-}"

report() {
  echo "privacy-scan: $*" >&2
  fail=1
}

check_untracked_path() {
  local path="$1"
  local found=0
  while IFS= read -r -d '' file; do
    if [[ -e "$file" ]]; then
      echo "$file" >&2
      found=1
    fi
  done < <(git ls-files -z "$path")
  if [[ "$found" -ne 0 ]]; then
    report "$path must not be tracked"
  fi
}

check_untracked_path ".env"
check_untracked_path "data"
check_untracked_path ".playwright-mcp"

while IFS= read -r -d '' file; do
  [[ -f "$file" ]] || continue
  case "$file" in
    .git/*|data/*|downloads/*|library/*|.playwright-mcp/*|*.png|*.jpg|*.jpeg|*.gif|*.webp|*.ico)
      continue
      ;;
  esac

  if grep -Iq . "$file"; then
    if [[ -n "$local_user" ]] && grep -nF "/home/${local_user}" "$file"; then
      report "$file contains a local home path"
    fi
    home_prefix="/home/"
    if grep -nE "(${home_prefix}|/Users/)[[:alnum:]_.-]+/" "$file"; then
      report "$file contains a local absolute home path"
    fi
    if [[ -n "$forbidden_namespace" ]] && grep -nF "$forbidden_namespace" "$file"; then
      report "$file contains a forbidden private namespace"
    fi
    if grep -nE '(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})' "$file"; then
      report "$file contains a token-like secret"
    fi
  fi
done < "$tracked_files"

if [[ "$fail" -ne 0 ]]; then
  echo "privacy-scan: failed" >&2
  exit 1
fi

echo "privacy-scan: ok"
