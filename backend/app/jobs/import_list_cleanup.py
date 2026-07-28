"""Maintenance for the .import-lists download->import handoff files.

A download job writes {DOWNLOAD_ROOT}/.import-lists/{import_job_id}.json as a
durable fallback for the import worker. The Redis copy self-expires (24h TTL),
but the disk copy is only removed by the legacy fallback consume path — which
the current PostgreSQL-ledger import flow normally bypasses, so the files leak.
These helpers delete a job's file on success and sweep stale orphans.
"""

from __future__ import annotations

import time
from pathlib import Path


def cleanup_import_list(import_lists_dir: Path, import_job_id: str) -> bool:
    """Delete one job's handoff file. Idempotent.

    Returns True if a file was removed, False if it was already gone.
    """
    path = import_lists_dir / f"{import_job_id}.json"
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def prune_import_lists(
    import_lists_dir: Path, max_age_seconds: int, now: float | None = None
) -> int:
    """Delete *.json handoff files older than max_age_seconds.

    Sweeps orphans left by imports that never consumed their list (failed or
    never-run jobs). Returns the number of files removed.
    """
    if not import_lists_dir.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    for path in import_lists_dir.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed
