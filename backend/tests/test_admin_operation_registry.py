"""One operation definition supplies dispatch, result and resource policy."""
from dataclasses import replace

import pytest

from app.services.operations import ADMIN_OPERATION_REGISTRY


@pytest.mark.parametrize("operation,payload,status,reason", [
    ("admin-clear", {"status": "partial", "message": "File removal failed"}, "failed", "media_clear_incomplete"),
    ("admin-clear", {"status": "ok"}, "complete", None),
    ("admin-cleanup-metadata-jsons", {"failed": 2, "message": "Two removals failed"}, "failed", "metadata_cleanup_partial_failure"),
    ("subscription-sync-batch", {"failed_count": 1}, "failed", "batch_partial_failure"),
    ("admin-creator-reenrich", {"aborted": True}, "failed", "operation_semantic_failure"),
    ("admin-search-reindex", {"status": "error"}, "failed", "operation_semantic_failure"),
    ("asset-dedup-scan", {"status": "superseded"}, "failed", "operation_superseded"),
    ("admin-backup-create", {"filename": "backup.tar.gz"}, "complete", None),
])
def test_registered_handler_result_policy(operation, payload, status, reason):
    result = ADMIN_OPERATION_REGISTRY[operation].normalize_result(payload)
    assert result.payload == payload
    assert result.status == status
    assert result.reason_code == reason


async def test_new_registered_handler_runs_without_extending_worker(monkeypatch):
    from app.jobs.admin_operations import _execute_registered_admin_operation
    from app.services import admin_operation_handlers

    observed = []

    async def handler(task_id, attempt, options):
        observed.append((task_id, attempt, options))
        return {"count": options["count"]}

    monkeypatch.setattr(admin_operation_handlers, "test_count", handler, raising=False)
    spec = replace(ADMIN_OPERATION_REGISTRY["admin-backup-estimate"],
                   operation_type="test-count", handler="test_count")
    monkeypatch.setitem(ADMIN_OPERATION_REGISTRY, "test-count", spec)
    result = await _execute_registered_admin_operation("test-count", "task", 3, {"count": 7})
    assert result.payload == {"count": 7}
    assert result.status == "complete"
    assert observed == [("task", 3, {"count": 7})]


def test_continuation_keeps_batch_counts_without_terminalizing():
    result = ADMIN_OPERATION_REGISTRY["subscription-sync-batch"].normalize_result({
        "_admin_handoff": True, "_admin_ready_successor": 4,
        "candidate_count": 5, "failed_count": 0,
    })
    assert result.continues
    assert result.payload["_admin_ready_successor"] == 4
    assert result.progress["current"] == result.progress["total"] == 5


@pytest.mark.parametrize("status", ["pending", "busy", "ok", "error"])
@pytest.mark.parametrize("handoff", [False, True])
async def test_search_handler_registry_worker_preserves_handoff(monkeypatch, status, handoff):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from uuid import uuid4
    from app.jobs import admin_operations as worker
    from app.services import operations, admin_operation_handlers as handlers

    payload = {"status": status, "message": status, "_admin_handoff": handoff}
    @asynccontextmanager
    async def lease(*args):
        yield None
    async def heartbeat(task_id, attempt, stop, db):
        await stop.wait()
    async def update(*args, **kwargs):
        # A handoff has advanced the durable attempt. Any old terminal write
        # would be rejected, which was the actual regression at this boundary.
        assert not handoff, "handoff must not write a terminal status"
        return True
    monkeypatch.setattr(operations, "admin_operation_execution_lease", lease)
    monkeypatch.setattr(operations, "claim_admin_operation", AsyncMock(return_value=("admin-search-reindex", {})))
    writer = AsyncMock(side_effect=update)
    monkeypatch.setattr(operations, "update_admin_task", writer)
    monkeypatch.setattr(worker, "_heartbeat_registered_admin_operation", heartbeat)
    monkeypatch.setattr(handlers, "_run_search_reindex_operation", AsyncMock(return_value=payload))
    result = await worker._run_registered_admin_operation_delivery(str(uuid4()), 1)
    assert result == payload
    if handoff:
        writer.assert_not_awaited()
        outcome = operations.ADMIN_OPERATION_REGISTRY["admin-search-reindex"].normalize_result(payload)
        assert outcome.continues and outcome.status == "running"
        assert outcome.error is None
    else:
        assert writer.await_args.kwargs["status"] == ("complete" if status == "ok" else "failed")
