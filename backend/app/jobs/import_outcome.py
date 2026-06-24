"""Pure outcome classification for the import job. No I/O — fully unit-testable."""
from __future__ import annotations

DEFAULT_IMPORT_SKIP_THRESHOLD = 0.5


def clamp_threshold(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def classify_import_outcome(
    *,
    total_groups: int,
    works: int,
    assets: int,
    existing: int,
    skipped: int,
    threshold: float,
) -> tuple[str, str]:
    """Classify an import run. Returns (status, message); status in {"complete","failed"}.

    Rules, in order:
      1. total_groups == 0           -> failed (nothing parsed)
      2. skipped > 0 and works == 0  -> failed (every parsed work failed)
      3. skip rate >= threshold      -> failed (provider/gallery-dl format drift)
      4. otherwise                   -> complete (includes all-existing no-op)
    """
    summary = f"works={works} assets={assets} existing={existing} skipped={skipped}"
    if total_groups == 0:
        return "failed", f"No parseable metadata groups found (0). {summary}"
    if skipped > 0 and works == 0:
        return "failed", f"All parsed works failed to import. {summary}"
    if skipped > 0 and (skipped / total_groups) >= threshold:
        pct = round(skipped / total_groups * 100)
        return "failed", (
            f"High import skip rate: {skipped}/{total_groups} ({pct}%); "
            f"may indicate provider/gallery-dl format change. {summary}"
        )
    return "complete", f"Import complete. {summary}"
