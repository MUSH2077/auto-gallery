"""Pure outcome classification for a download that wrote no new metadata JSONs."""
from __future__ import annotations


def classify_no_metadata_outcome(
    *,
    image_count: int,
    auth_warning: str | None,
    is_subscription: bool,
) -> tuple[str, str | None]:
    """Decide status when gallery-dl exits 0 but produced 0 new metadata JSONs.

    Returns (status, error_message); status in {"complete","failed"}.
      - auth warning + 0 media           -> failed (auth/restricted)
      - manual download + 0 media        -> failed (genuinely produced nothing)
      - subscription re-sync + 0 media   -> complete (no new content is normal)
      - media present but no metadata     -> complete (legacy behaviour preserved)
    """
    if image_count == 0 and auth_warning:
        return "failed", f"Download produced 0 files: {auth_warning}"
    if image_count == 0 and not is_subscription:
        return "failed", "gallery-dl exited 0 but produced no artifacts"
    if image_count == 0:
        return "complete", "No new content since last sync (already archived or empty)"
    return "complete", None
