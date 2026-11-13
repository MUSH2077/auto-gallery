"""Normalized outcomes returned by registered administrator handlers."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AdminOperationResult:
    payload: dict
    status: str = "complete"
    error: str | None = None
    reason_code: str | None = None
    progress: dict = field(default_factory=dict)
    continues: bool = False


def success(payload: dict) -> AdminOperationResult:
    return AdminOperationResult(payload, continues=bool(payload.get("_admin_handoff")))


def failure(payload: dict, message: str, reason: str = "operation_semantic_failure") -> AdminOperationResult:
    return AdminOperationResult(payload, status="failed", error=message, reason_code=reason)


def metadata_cleanup(payload: dict) -> AdminOperationResult:
    if payload.get("failed", 0):
        return failure(payload, str(payload["message"]), "metadata_cleanup_partial_failure")
    return success(payload)


def media_clear(payload: dict) -> AdminOperationResult:
    if payload.get("status") in {"partial", "failed"}:
        return failure(payload, str(payload.get("message") or "Media clear incomplete"), "media_clear_incomplete")
    return success(payload)


def subscription_batch(payload: dict) -> AdminOperationResult:
    progress = {**{key: value for key, value in payload.items() if key.endswith("_count")},
                "current": payload.get("candidate_count", 0), "total": payload.get("candidate_count", 0)}
    return AdminOperationResult(
        payload, status="failed" if payload.get("failed_count", 0) else "complete",
        error="Some subscription sources failed" if payload.get("failed_count", 0) else None,
        reason_code="batch_partial_failure" if payload.get("failed_count", 0) else None,
        progress=progress, continues=bool(payload.get("_admin_handoff")),
    )


def creator_refresh(payload: dict) -> AdminOperationResult:
    if payload.get("aborted"):
        return failure(payload, str(payload.get("abort_reason") or "creator mapping refresh aborted"))
    return success(payload)


def search_reindex(payload: dict) -> AdminOperationResult:
    if "status" in payload and payload.get("status") != "ok":
        return failure(payload, str(payload.get("message") or "search reindex failed"))
    return success(payload)


def asset_dedup(payload: dict) -> AdminOperationResult:
    if payload.get("status") in {"failed", "superseded"}:
        status = payload["status"]
        return failure(payload, str(payload.get("message") or f"asset dedup scan {status}"),
                       "operation_superseded" if status == "superseded" else "operation_semantic_failure")
    return success(payload)
