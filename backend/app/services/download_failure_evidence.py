"""Attempt-scoped evidence for an exhausted upstream download failure."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.job_manifest import get_manifest, update_manifest


EVIDENCE_KEY = "unresolved_provider_failure"
EVIDENCE_VERSION = 1
PROVIDER_FAILURE_KINDS = frozenset({"authentication", "nonzero", "timeout", "unexpected"})


def record_unresolved_provider_failure(
    job,
    *,
    kind: str,
    reason: str,
    max_retries: int,
) -> dict[str, Any]:
    if kind not in PROVIDER_FAILURE_KINDS:
        raise ValueError(f"unsupported provider failure kind: {kind}")
    evidence = {
        "version": EVIDENCE_VERSION,
        "state": "unresolved",
        "kind": kind,
        "reason": str(reason)[:5000],
        "retry_count": int(job.retry_count or 0),
        "max_retries": int(max_retries),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    update_manifest(job, **{EVIDENCE_KEY: evidence})
    return evidence


def unresolved_provider_failure(job) -> dict[str, Any] | None:
    evidence = get_manifest(job).get(EVIDENCE_KEY)
    if not isinstance(evidence, dict):
        return None
    try:
        retry_count = int(evidence.get("retry_count"))
        max_retries = int(evidence.get("max_retries"))
    except (TypeError, ValueError):
        return None
    reason = evidence.get("reason")
    if (
        evidence.get("version") != EVIDENCE_VERSION
        or evidence.get("state") != "unresolved"
        or evidence.get("kind") not in PROVIDER_FAILURE_KINDS
        or not isinstance(reason, str)
        or not reason.strip()
        or retry_count != int(job.retry_count or 0)
        or max_retries < 0
        or retry_count < max_retries
    ):
        return None
    return dict(evidence)


def clear_unresolved_provider_failure(job) -> None:
    manifest = get_manifest(job)
    if EVIDENCE_KEY in manifest:
        manifest.pop(EVIDENCE_KEY, None)
        job.manifest = manifest
