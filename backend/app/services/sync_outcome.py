"""Structured, non-error outcomes for successful download synchronizations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, TypedDict

from app.services.job_manifest import update_manifest


SyncOutcomeCode = Literal["new_content", "no_changes", "no_content"]


class SyncOutcome(TypedDict):
    code: SyncOutcomeCode
    metadata_count: int
    media_count: int
    completed_at: str


VALID_SYNC_OUTCOME_CODES = frozenset({"new_content", "no_changes", "no_content"})
LEGACY_NO_CHANGES_MESSAGE = "No new content since last sync (already archived or empty)"
LEGACY_ZERO_ARTIFACTS_MESSAGE = "Found 0 metadata files and 0 media files"


def build_sync_outcome(
    code: SyncOutcomeCode,
    *,
    metadata_count: int,
    media_count: int,
    completed_at: datetime | None = None,
) -> SyncOutcome:
    """Return the stable result contract shared by domain and task projections."""
    timestamp = completed_at or datetime.now(timezone.utc)
    return {
        "code": code,
        "metadata_count": max(0, int(metadata_count)),
        "media_count": max(0, int(media_count)),
        "completed_at": timestamp.isoformat(),
    }


def sync_outcome_from_manifest(manifest: dict | None) -> SyncOutcome | None:
    value = (manifest or {}).get("outcome")
    if not isinstance(value, dict) or value.get("code") not in VALID_SYNC_OUTCOME_CODES:
        return None
    try:
        return {
            "code": value["code"],
            "metadata_count": max(0, int(value.get("metadata_count", 0))),
            "media_count": max(0, int(value.get("media_count", 0))),
            "completed_at": str(value["completed_at"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def download_job_outcome(job) -> SyncOutcome | None:
    return sync_outcome_from_manifest(getattr(job, "manifest", None))


def set_download_job_outcome(job, outcome: SyncOutcome) -> SyncOutcome:
    update_manifest(job, outcome=outcome)
    return outcome


def clear_download_job_outcome(job) -> None:
    manifest = dict(getattr(job, "manifest", None) or {})
    if "outcome" not in manifest:
        return
    manifest.pop("outcome", None)
    job.manifest = manifest


def had_sync_baseline(job) -> bool:
    """Read the baseline captured when this execution was enqueued."""
    return bool((getattr(job, "manifest", None) or {}).get("had_sync_baseline", False))
