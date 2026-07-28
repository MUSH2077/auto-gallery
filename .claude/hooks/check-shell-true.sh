#!/usr/bin/env bash
# PreToolUse(Edit|Write|MultiEdit): block introduction of `shell=True`.
# Enforces the project's #1 security rule: gallery-dl and all subprocess calls
# must use shell=False with an argument list (.claude/constraints/security.md).
# Only inspects Python edits; exit 2 blocks the tool call.
set -euo pipefail

input="$(cat)"
fp="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')"
case "$fp" in
  *.py) ;;
  *) exit 0 ;;
esac

# Gather every chunk of new content across Write (.content),
# Edit (.new_string), and MultiEdit (.edits[].new_string).
content="$(printf '%s' "$input" | jq -r '
  [ .tool_input.content,
    .tool_input.new_string,
    (.tool_input.edits[]?.new_string) ]
  | map(select(. != null)) | join("\n")')"

if printf '%s' "$content" | grep -qE 'shell[[:space:]]*=[[:space:]]*True'; then
  echo "BLOCKED: 'shell=True' detected in a Python edit. .claude/constraints/security.md forbids shell=True — it is a command-injection vector for gallery-dl URLs. Use subprocess.Popen([...], shell=False) with an argument list." >&2
  exit 2
fi

exit 0
