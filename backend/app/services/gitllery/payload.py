from __future__ import annotations

from collections.abc import Sequence

from app.models import CurationChange, CurationCommit


def build_segment_commit_payload(
    commit: CurationCommit,
    changes: Sequence[CurationChange],
    *,
    repository_parent_commit_id: str | None,
) -> dict:
    """Build one portable segment commit from authoritative domain rows."""

    return {
        "commit_id": str(commit.id),
        # Each portable repository has its own complete chain. Keep the
        # authoritative global parent separately for audit/debugging.
        "parent_commit_id": repository_parent_commit_id,
        "authoritative_parent_commit_id": (
            str(commit.parent_commit_id) if commit.parent_commit_id else None
        ),
        "actor_type": commit.actor_type,
        "actor_id": commit.actor_id,
        "message": commit.message,
        "trigger": commit.trigger,
        "dedupe_key": commit.dedupe_key,
        "reverts_commit_id": (
            str(commit.reverts_commit_id) if commit.reverts_commit_id else None
        ),
        "occurred_at": commit.occurred_at.isoformat() if commit.occurred_at else None,
        "created_at": commit.created_at.isoformat(),
        "stats": commit.stats or {},
        "metadata": commit.extra_metadata or {},
        "changes": [
            {
                "change_id": str(change.id),
                "subject_type": change.subject_type,
                "subject_id": change.subject_id,
                "action": change.action,
                "sequence": change.sequence,
                "before_state": change.before_state,
                "after_state": change.after_state,
                "diff": change.diff or {},
                "impact": change.impact or {},
            }
            for change in changes
        ],
    }
