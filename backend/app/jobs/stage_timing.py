"""Time a pipeline stage and record it as a manifest event."""
from __future__ import annotations

import time
from contextlib import contextmanager

from app.services.job_manifest import append_manifest_event


@contextmanager
def stage_timer(job, stage: str):
    """Append a `stage_timing` event (stage, ms) to job.manifest on exit.

    The caller is responsible for persisting (committing) `job` afterwards.
    """
    start = time.monotonic()
    try:
        yield
    finally:
        ms = int((time.monotonic() - start) * 1000)
        append_manifest_event(job, "stage_timing", stage=stage, ms=ms)
