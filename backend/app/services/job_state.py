"""Backward-compatible job state imports.

The state machine now lives with the task domain model. Keep this thin module
for integrations and maintenance scripts that still import the former service
path.
"""

from app.models.task_state import (
    InvalidTaskTransition,
    transition_download_job,
    transition_import_job,
)

InvalidJobTransition = InvalidTaskTransition

__all__ = [
    "InvalidJobTransition",
    "InvalidTaskTransition",
    "transition_download_job",
    "transition_import_job",
]
