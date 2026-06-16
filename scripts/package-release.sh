#!/usr/bin/env bash
set -euo pipefail

version="${1:-$(git describe --tags --always --dirty 2>/dev/null || echo dev)}"
name="auto-gallery-${version}"
out_dir="${RELEASE_OUT_DIR:-dist}"
archive="${out_dir}/${name}.tar.gz"

mkdir -p "$out_dir"
rm -f "$archive"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/${name}"
file_list="$tmp/files.txt"
while IFS= read -r -d '' file; do
  [[ -e "$file" ]] && printf '%s\0' "$file"
done < <(git ls-files --cached -z) > "$file_list"
tar --null -cf - --files-from "$file_list" | tar -x -C "$tmp/${name}"

rm -rf \
  "$tmp/${name}/.claude" \
  "$tmp/${name}/.github/ISSUE_TEMPLATE" \
  "$tmp/${name}/.playwright-mcp" \
  "$tmp/${name}/data" \
  "$tmp/${name}/downloads" \
  "$tmp/${name}/library" \
  "$tmp/${name}/app-config" \
  "$tmp/${name}/gallerydl-config" \
  "$tmp/${name}/logs" \
  "$tmp/${name}/tmp" \
  "$tmp/${name}/temp" \
  "$tmp/${name}/.cache"

find "$tmp/${name}" \( \
  -name '.env' -o \
  -name '.env.*' -o \
  -name '*.log' -o \
  -name '.pytest_cache' -o \
  -name '__pycache__' -o \
  -name 'node_modules' -o \
  -name '.next' \
\) -prune -exec rm -rf {} +

tar -C "$tmp" -czf "$archive" "$name"
echo "$archive"
