"""
Task state machine — canonical status constants, allowed transitions, and
validation for both download and import pipeline jobs.

Replaces the ad-hoc string constants in services/job_state.py with a
structured state machine that supports:
  - True pause (signal-based, from any non-terminal state)
  - True cancel (terminal state, keeps DB record for audit)
  - Explicit pipeline stages
  - Priority tiers
  - Backward compatibility with old status strings
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final

# ──────────────────────────────────────────────
# Download pipeline statuses
# ──────────────────────────────────────────────
DOWNLOAD_ENQUEUED: Final = "enqueued"          # waiting in RQ queue (was "pending")
DOWNLOAD_DOWNLOADING: Final = "downloading"     # gallery-dl subprocess running
DOWNLOAD_DOWNLOADED: Final = "downloaded"       # gallery-dl finished, import not yet started
DOWNLOAD_IMPORTING: Final = "importing"         # import job created, import worker processing
DOWNLOAD_COMPLETE: Final = "complete"           # both download + import succeeded
DOWNLOAD_PAUSED: Final = "paused"               # user paused; signal sent to worker
DOWNLOAD_CANCELLED: Final = "cancelled"         # user cancelled; terminal (keeps DB record)
DOWNLOAD_FAILED: Final = "failed"               # retries exhausted; permanent failure
DOWNLOAD_STALE: Final = "stale"                 # worker died / heartbeat lost; recoverable

# ──────────────────────────────────────────────
# Import pipeline statuses
# ──────────────────────────────────────────────
IMPORT_ENQUEUED: Final = "enqueued"            # waiting in RQ queue (was "pending")
IMPORT_RUNNING: Final = "running"               # import worker active
IMPORT_COMPLETE: Final = "complete"             # import finished successfully
IMPORT_PAUSED: Final = "paused"                 # user paused
IMPORT_CANCELLED: Final = "cancelled"           # user cancelled; terminal
IMPORT_FAILED: Final = "failed"                 # retries exhausted; permanent failure
IMPORT_STALE: Final = "stale"                   # worker died / heartbeat lost

# ──────────────────────────────────────────────
# Status sets
# ──────────────────────────────────────────────
DOWNLOAD_TERMINAL_STATUSES: Final = frozenset({
    DOWNLOAD_COMPLETE,
    DOWNLOAD_CANCELLED,
    DOWNLOAD_FAILED,
})

DOWNLOAD_RUNNING_STATUSES: Final = frozenset({
    DOWNLOAD_ENQUEUED,
    DOWNLOAD_DOWNLOADING,
    DOWNLOAD_DOWNLOADED,
    DOWNLOAD_IMPORTING,
})

DOWNLOAD_RETRYABLE_STATUSES: Final = frozenset({
    DOWNLOAD_FAILED,
    DOWNLOAD_STALE,
    DOWNLOAD_DOWNLOADING,
    DOWNLOAD_COMPLETE,
})

DOWNLOAD_PAUSABLE_STATUSES: Final = frozenset({
    DOWNLOAD_ENQUEUED,
    DOWNLOAD_DOWNLOADING,
    DOWNLOAD_DOWNLOADED,
    DOWNLOAD_IMPORTING,
    DOWNLOAD_FAILED,
    DOWNLOAD_STALE,
})

DOWNLOAD_CANCELLABLE_STATUSES: Final = frozenset({
    DOWNLOAD_ENQUEUED,
    DOWNLOAD_DOWNLOADING,
    DOWNLOAD_DOWNLOADED,
    DOWNLOAD_IMPORTING,
    DOWNLOAD_PAUSED,
    DOWNLOAD_FAILED,
    DOWNLOAD_STALE,
})

IMPORT_TERMINAL_STATUSES: Final = frozenset({
    IMPORT_COMPLETE,
    IMPORT_CANCELLED,
    IMPORT_FAILED,
})

IMPORT_PAUSABLE_STATUSES: Final = frozenset({
    IMPORT_ENQUEUED,
    IMPORT_RUNNING,
    IMPORT_FAILED,
    IMPORT_STALE,
})

IMPORT_CANCELLABLE_STATUSES: Final = frozenset({
    IMPORT_ENQUEUED,
    IMPORT_RUNNING,
    IMPORT_PAUSED,
    IMPORT_FAILED,
    IMPORT_STALE,
})

# ──────────────────────────────────────────────
# Allowed transitions
# ──────────────────────────────────────────────
ALLOWED_DOWNLOAD_TRANSITIONS: Final = {
    DOWNLOAD_ENQUEUED:     {DOWNLOAD_DOWNLOADING, DOWNLOAD_PAUSED, DOWNLOAD_CANCELLED, DOWNLOAD_FAILED, DOWNLOAD_STALE},
    DOWNLOAD_DOWNLOADING:  {DOWNLOAD_DOWNLOADED, DOWNLOAD_PAUSED, DOWNLOAD_CANCELLED, DOWNLOAD_FAILED, DOWNLOAD_STALE},
    DOWNLOAD_DOWNLOADED:   {DOWNLOAD_IMPORTING, DOWNLOAD_COMPLETE, DOWNLOAD_PAUSED, DOWNLOAD_CANCELLED, DOWNLOAD_FAILED, DOWNLOAD_STALE},
    DOWNLOAD_IMPORTING:    {DOWNLOAD_COMPLETE, DOWNLOAD_PAUSED, DOWNLOAD_CANCELLED, DOWNLOAD_FAILED, DOWNLOAD_STALE},
    DOWNLOAD_PAUSED:       {DOWNLOAD_ENQUEUED, DOWNLOAD_CANCELLED, DOWNLOAD_FAILED, DOWNLOAD_STALE},
    DOWNLOAD_FAILED:       {DOWNLOAD_ENQUEUED, DOWNLOAD_CANCELLED, DOWNLOAD_IMPORTING},
    DOWNLOAD_STALE:        {DOWNLOAD_ENQUEUED, DOWNLOAD_CANCELLED, DOWNLOAD_IMPORTING},
    DOWNLOAD_COMPLETE:     {DOWNLOAD_ENQUEUED},
    DOWNLOAD_CANCELLED:    frozenset(),
}

ALLOWED_IMPORT_TRANSITIONS: Final = {
    IMPORT_ENQUEUED:  {IMPORT_RUNNING, IMPORT_PAUSED, IMPORT_CANCELLED, IMPORT_FAILED, IMPORT_STALE},
    IMPORT_RUNNING:   {IMPORT_COMPLETE, IMPORT_PAUSED, IMPORT_CANCELLED, IMPORT_FAILED, IMPORT_STALE},
    IMPORT_PAUSED:    {IMPORT_ENQUEUED, IMPORT_CANCELLED, IMPORT_FAILED, IMPORT_STALE},
    IMPORT_FAILED:    {IMPORT_ENQUEUED, IMPORT_CANCELLED},
    IMPORT_STALE:     {IMPORT_ENQUEUED, IMPORT_CANCELLED},
    IMPORT_COMPLETE:  frozenset(),
    IMPORT_CANCELLED: frozenset(),
}

# ──────────────────────────────────────────────
# Backward compatibility — old → new status map
# ──────────────────────────────────────────────
OLD_TO_NEW_DOWNLOAD: Final = {
    "pending": DOWNLOAD_ENQUEUED,
    "downloading": DOWNLOAD_DOWNLOADING,
    "downloaded": DOWNLOAD_DOWNLOADED,
    "importing": DOWNLOAD_IMPORTING,
    "complete": DOWNLOAD_COMPLETE,
    "paused": DOWNLOAD_PAUSED,
    "failed": DOWNLOAD_FAILED,
    "stale": DOWNLOAD_STALE,
}

OLD_TO_NEW_IMPORT: Final = {
    "pending": IMPORT_ENQUEUED,
    "running": IMPORT_RUNNING,
    "complete": IMPORT_COMPLETE,
    "failed": IMPORT_FAILED,
    "stale": IMPORT_STALE,
}

# Old aliases kept for code that still references the old status string "pending"
DOWNLOAD_PENDING = DOWNLOAD_ENQUEUED
IMPORT_PENDING = IMPORT_ENQUEUED

# ──────────────────────────────────────────────
# Priority tiers
# ──────────────────────────────────────────────
class TaskPriority(IntEnum):
    LOW = 0
    NORMAL = 10
    HIGH = 20
    CRITICAL = 30

    @classmethod
    def label(cls, value: int) -> str:
        labels = {0: "low", 10: "normal", 20: "high", 30: "critical"}
        return labels.get(value, f"unknown({value})")


# ──────────────────────────────────────────────
# Pipeline stages (used for progress reporting)
# ──────────────────────────────────────────────
class PipelineStage:
    """Explicit pipeline stages for download → import flow."""
    ENQUEUED = "enqueued"
    CONFIGURING = "configuring"
    DOWNLOADING = "downloading"
    POST_DOWNLOAD = "post_download"
    ENQUEUING_IMPORT = "enqueuing_import"
    IMPORTING = "importing"
    IMPORT_INDEXING = "import_indexing"
    COMPLETE = "complete"

    @classmethod
    def ordered_stages(cls) -> list[str]:
        return [
            cls.ENQUEUED,
            cls.CONFIGURING,
            cls.DOWNLOADING,
            cls.POST_DOWNLOAD,
            cls.ENQUEUING_IMPORT,
            cls.IMPORTING,
            cls.IMPORT_INDEXING,
            cls.COMPLETE,
        ]


# ──────────────────────────────────────────────
# Transition validation
# ──────────────────────────────────────────────
class InvalidTaskTransition(ValueError):
    """Raised when an invalid status transition is attempted."""
    pass


def _normalize_status(status: str, mapping: dict[str, str]) -> str:
    """Map old status strings to new ones for backward compat."""
    return mapping.get(status, status)


def _transition(
    job,
    allowed: dict[str, frozenset[str]],
    new_status: str,
    error_log: str | None = None,
    status_mapping: dict[str, str] | None = None,
):
    if status_mapping:
        new_status = _normalize_status(new_status, status_mapping)

    old_status = job.status
    if status_mapping:
        old_status = _normalize_status(old_status, status_mapping)

    if old_status == new_status:
        if error_log is not None:
            job.error_log = error_log
        return job

    allowed_targets = allowed.get(old_status, frozenset())
    if new_status not in allowed_targets:
        raise InvalidTaskTransition(
            f"Cannot transition '{old_status}' -> '{new_status}' "
            f"(allowed: {sorted(allowed_targets) or 'none'})"
        )

    job.status = new_status
    if error_log is not None:
        job.error_log = error_log
    return job


def transition_download_job(job, new_status: str, error_log: str | None = None):
    """Validate and apply a download job status transition."""
    return _transition(
        job,
        ALLOWED_DOWNLOAD_TRANSITIONS,
        new_status,
        error_log,
        status_mapping=OLD_TO_NEW_DOWNLOAD,
    )


def transition_import_job(job, new_status: str, error_log: str | None = None):
    """Validate and apply an import job status transition."""
    return _transition(
        job,
        ALLOWED_IMPORT_TRANSITIONS,
        new_status,
        error_log,
        status_mapping=OLD_TO_NEW_IMPORT,
    )
