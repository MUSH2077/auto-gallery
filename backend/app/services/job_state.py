from __future__ import annotations

from typing import Final


DOWNLOAD_PENDING: Final = "pending"
DOWNLOAD_DOWNLOADING: Final = "downloading"
DOWNLOAD_DOWNLOADED: Final = "downloaded"
DOWNLOAD_IMPORTING: Final = "importing"
DOWNLOAD_COMPLETE: Final = "complete"
DOWNLOAD_FAILED: Final = "failed"
DOWNLOAD_STALE: Final = "stale"
DOWNLOAD_PAUSED: Final = "paused"

IMPORT_PENDING: Final = "pending"
IMPORT_RUNNING: Final = "running"
IMPORT_COMPLETE: Final = "complete"
IMPORT_FAILED: Final = "failed"
IMPORT_STALE: Final = "stale"

DOWNLOAD_RUNNING_STATUSES: Final = (
    DOWNLOAD_PENDING,
    DOWNLOAD_DOWNLOADING,
    DOWNLOAD_DOWNLOADED,
    DOWNLOAD_IMPORTING,
)

DOWNLOAD_RETRYABLE_STATUSES: Final = (
    DOWNLOAD_FAILED,
    DOWNLOAD_STALE,
    DOWNLOAD_DOWNLOADING,
    DOWNLOAD_COMPLETE,
)

ALLOWED_DOWNLOAD_TRANSITIONS: Final = {
    DOWNLOAD_PENDING: {DOWNLOAD_DOWNLOADING, DOWNLOAD_IMPORTING, DOWNLOAD_PAUSED, DOWNLOAD_FAILED, DOWNLOAD_STALE},
    DOWNLOAD_DOWNLOADING: {DOWNLOAD_DOWNLOADED, DOWNLOAD_IMPORTING, DOWNLOAD_PENDING, DOWNLOAD_FAILED, DOWNLOAD_STALE, DOWNLOAD_PAUSED},
    DOWNLOAD_DOWNLOADED: {DOWNLOAD_IMPORTING, DOWNLOAD_COMPLETE, DOWNLOAD_FAILED, DOWNLOAD_STALE},
    DOWNLOAD_IMPORTING: {DOWNLOAD_COMPLETE, DOWNLOAD_FAILED, DOWNLOAD_STALE},
    DOWNLOAD_PAUSED: {DOWNLOAD_PENDING, DOWNLOAD_FAILED, DOWNLOAD_STALE},
    DOWNLOAD_FAILED: {DOWNLOAD_PENDING, DOWNLOAD_IMPORTING},
    DOWNLOAD_STALE: {DOWNLOAD_PENDING, DOWNLOAD_IMPORTING},
    DOWNLOAD_COMPLETE: {DOWNLOAD_PENDING},
}

ALLOWED_IMPORT_TRANSITIONS: Final = {
    IMPORT_PENDING: {IMPORT_RUNNING, IMPORT_FAILED, IMPORT_STALE},
    IMPORT_RUNNING: {IMPORT_COMPLETE, IMPORT_PENDING, IMPORT_FAILED, IMPORT_STALE},
    IMPORT_FAILED: {IMPORT_PENDING},
    IMPORT_STALE: {IMPORT_PENDING},
    IMPORT_COMPLETE: set(),
}


class InvalidJobTransition(ValueError):
    pass


def _transition(job, allowed: dict[str, set[str]], new_status: str, error_log: str | None = None):
    old_status = job.status
    if old_status == new_status:
        if error_log is not None:
            job.error_log = error_log
        return job
    if new_status not in allowed.get(old_status, set()):
        raise InvalidJobTransition(f"Cannot transition job from '{old_status}' to '{new_status}'")
    job.status = new_status
    if error_log is not None:
        job.error_log = error_log
    return job


def transition_download_job(job, new_status: str, error_log: str | None = None):
    return _transition(job, ALLOWED_DOWNLOAD_TRANSITIONS, new_status, error_log)


def transition_import_job(job, new_status: str, error_log: str | None = None):
    return _transition(job, ALLOWED_IMPORT_TRANSITIONS, new_status, error_log)
