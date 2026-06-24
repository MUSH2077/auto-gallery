#!/usr/bin/env bash
# Stop hook: gentle, NON-blocking reminder of the MANDATORY Completion Workflow
# (CLAUDE.md) when code under backend/ or admin-web/ is left uncommitted.
# Always exits 0 — never blocks the agent from stopping.
set -uo pipefail

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

changes="$(git status --porcelain -- backend admin-web 2>/dev/null || true)"
if [ -n "$changes" ]; then
  echo "Reminder — uncommitted changes under backend/ or admin-web/. Completion Workflow (CLAUDE.md): 1) docker compose exec backend python -m pytest  2) rebuild + restart (bash scripts/deploy.sh)  3) git commit." >&2
fi

exit 0
