"""
Backward-compatibility re-exports from the canonical task state machine.

New code should import from ``app.models.task_state`` directly.
This module exists so existing importers continue to work during
the phased migration.
"""

from __future__ import annotations

# Re-export everything from the canonical location
from app.models.task_state import (
    # Download statuses
    DOWNLOAD_ENQUEUED,
    DOWNLOAD_DOWNLOADING,
    DOWNLOAD_DOWNLOADED,
    DOWNLOAD_IMPORTING,
    DOWNLOAD_COMPLETE,
    DOWNLOAD_PAUSED,
    DOWNLOAD_CANCELLED,
    DOWNLOAD_FAILED,
    DOWNLOAD_STALE,
    # Import statuses
    IMPORT_ENQUEUED,
    IMPORT_RUNNING,
    IMPORT_COMPLETE,
    IMPORT_PAUSED,
    IMPORT_CANCELLED,
    IMPORT_FAILED,
    IMPORT_STALE,
    # Sets
    DOWNLOAD_TERMINAL_STATUSES,
    DOWNLOAD_RUNNING_STATUSES,
    DOWNLOAD_RETRYABLE_STATUSES,
    DOWNLOAD_PAUSABLE_STATUSES,
    DOWNLOAD_CANCELLABLE_STATUSES,
    IMPORT_TERMINAL_STATUSES,
    IMPORT_PAUSABLE_STATUSES,
    IMPORT_CANCELLABLE_STATUSES,
    # Transition maps
    ALLOWED_DOWNLOAD_TRANSITIONS,
    ALLOWED_IMPORT_TRANSITIONS,
    # Transition functions
    transition_download_job,
    transition_import_job,
    InvalidTaskTransition,
    # Priority
    TaskPriority,
    # Pipeline
    PipelineStage,
    # Backward-compat aliases
    OLD_TO_NEW_DOWNLOAD,
    OLD_TO_NEW_IMPORT,
    DOWNLOAD_PENDING,
    IMPORT_PENDING,
)

# Old exception name alias
InvalidJobTransition = InvalidTaskTransition
