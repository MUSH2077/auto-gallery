"""Pure outcome classification for a download that wrote no metadata JSONs."""
from __future__ import annotations

from dataclasses import dataclass

from app.services.sync_outcome import SyncOutcomeCode


@dataclass(frozen=True)
class NoMetadataDecision:
    status: str
    error: str | None = None
    outcome_code: SyncOutcomeCode | None = None


def classify_no_metadata_outcome(
    *,
    image_count: int,
    auth_warning: str | None,
    is_subscription: bool,
    had_sync_baseline: bool = False,
) -> NoMetadataDecision:
    """Decide lifecycle status and business result after a zero-metadata run."""
    if image_count == 0 and auth_warning:
        return NoMetadataDecision("failed", f"Download produced 0 files: {auth_warning}")
    if image_count == 0 and not is_subscription:
        return NoMetadataDecision("failed", "gallery-dl exited 0 but produced no artifacts")
    if is_subscription:
        return NoMetadataDecision(
            "complete",
            outcome_code="no_changes" if had_sync_baseline else "no_content",
        )
    return NoMetadataDecision("complete")
