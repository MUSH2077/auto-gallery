"""Asynchronous administrator diagnostics and backup operation regressions."""

from __future__ import annotations

import asyncio
import gc
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text


PREFIX = "slow_admin_ops_"


async def _clear_rows(db) -> None:
    from app.models import TaskEvent, TaskRun

    await db.execute(delete(TaskEvent))
    await db.execute(delete(TaskRun).where(TaskRun.kind == "admin"))
    await db.execute(text("DELETE FROM users WHERE username LIKE :prefix"), {"prefix": f"{PREFIX}%"})
    await db.commit()


async def _seed_user(db, username: str, *, permissions: list[str]) -> None:
    from app.auth import hash_password
    from app.models.user import User

    db.add(
        User(
            username=username,
            password_hash=hash_password("hunter22"),
            is_admin=False,
            is_active=True,
            permissions=permissions,
            must_change_password=False,
        )
    )
    await db.commit()


def _headers(username: str) -> dict[str, str]:
    from app.auth import create_access_token

    token = create_access_token(username, must_change_password=False)
    return {"Authorization": f"Bearer {token}"}


def _stub_rq_transport(monkeypatch) -> None:
    from app.services import operations

    monkeypatch.setattr(operations, "_fetch_admin_rq", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        operations,
        "_enqueue_admin_rq",
        lambda *_args, rq_job_id, **_kwargs: SimpleNamespace(id=rq_job_id),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slow_admin_actions_are_authorized_bounded_202_task_starts(monkeypatch):
    """A request regression cannot re-enter recursive, network, or backup work."""
    from app.api.admin import backup, gallerydl, settings as settings_api
    from app.database import async_session, engine
    from app.main import app

    _stub_rq_transport(monkeypatch)

    def forbidden_sync(*_args, **_kwargs):
        raise AssertionError("slow work ran in the request handler")

    async def forbidden_async(*_args, **_kwargs):
        raise AssertionError("slow work ran in the request handler")

    monkeypatch.setattr(backup, "_estimate_component_sizes", forbidden_sync)
    monkeypatch.setattr(backup, "_create_backup_sync", forbidden_sync)
    monkeypatch.setattr(settings_api, "_run_integrity_check", forbidden_async)
    monkeypatch.setattr(settings_api, "_get_setting", forbidden_async)
    monkeypatch.setattr(gallerydl.subprocess, "run", forbidden_sync)

    starts = [
        ("/api/v1/admin/integrity-check", None, "admin-integrity-scan"),
        ("/api/v1/admin/backup/estimate", None, "admin-backup-estimate"),
        (
            "/api/v1/admin/backup",
            {"contents": ["database", "gallerydl-config"]},
            "admin-backup-create",
        ),
        ("/api/v1/admin/proxy/test", None, "admin-proxy-test"),
        (
            "/api/v1/admin/gallerydl-config/test-connection",
            {"source": "pixiv"},
            "admin-gallerydl-connectivity-test",
        ),
    ]
    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear_rows(db)
            await _seed_user(db, f"{PREFIX}system", permissions=["system"])
            await _seed_user(db, f"{PREFIX}tasks", permissions=["tasks"])

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for path, body, operation_type in starts:
                response = await asyncio.wait_for(
                    client.post(
                        path,
                        headers=_headers(f"{PREFIX}system"),
                        **({"json": body} if body is not None else {}),
                    ),
                    timeout=2.0,
                )
                assert response.status_code == 202, (path, response.status_code, response.text)
                payload = response.json()
                assert set(payload) == {"task_id", "job_id", "status", "operation_type"}
                assert payload["status"] == "enqueued"
                assert payload["operation_type"] == operation_type
                assert payload["job_id"] == f"admin-{payload['task_id']}-attempt-1"

                denied = await client.post(
                    path,
                    headers=_headers(f"{PREFIX}tasks"),
                    **({"json": body} if body is not None else {}),
                )
                assert denied.status_code == 403

                anonymous = await client.post(
                    path,
                    **({"json": body} if body is not None else {}),
                )
                assert anonymous.status_code == 401
    finally:
        async with async_session() as db:
            await _clear_rows(db)
        await engine.dispose()


@pytest.mark.asyncio
async def test_memory_diagnostics_uses_bounded_proc_and_pool_metrics(monkeypatch):
    """Removing the GC census keeps this request independent of heap size."""
    from app.api.admin import settings as settings_api

    def forbidden(*_args, **_kwargs):
        raise AssertionError("memory diagnostics entered a full GC census")

    monkeypatch.setattr(gc, "collect", forbidden)
    monkeypatch.setattr(gc, "get_objects", forbidden)

    result = await settings_api.memory_diagnostics()

    assert result["rss_mb"] is None or result["rss_mb"] >= 0
    assert result["source"] == "/proc/self/status"
    assert {"size", "max_overflow", "checked_in", "checked_out", "overflow"} <= set(result["pool"])
    assert "total_tracked_objects" not in result
    assert "top_types" not in result
    assert "gc_counts" not in result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_latest_snapshot_ignores_failed_runs_and_is_scoped(monkeypatch):
    """A newer failed probe cannot replace the last usable per-source result."""
    from app.database import async_session, engine
    from app.main import app
    from app.services import operations
    from app.services.tasks import TaskService

    _stub_rq_transport(monkeypatch)
    old_finished = datetime.now(timezone.utc) - timedelta(minutes=5)
    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear_rows(db)
            await _seed_user(db, f"{PREFIX}snapshot", permissions=["system"])

            successful = await operations.prepare_admin_operation(
                db,
                operation_type="admin-gallerydl-connectivity-test",
                scope_key="diagnostics:gallerydl:pixiv",
                title="Test Pixiv connectivity",
                entity="gallerydl-connectivity",
                options={"source": "pixiv"},
            )
            await TaskService(db).update_task(
                successful.task,
                status="complete",
                progress={"phase": "complete", "label": "Pixiv test complete"},
                result={"source": "pixiv", "success": True, "message": "usable snapshot"},
            )
            successful.task.finished_at = old_finished
            await db.commit()

            failed = await operations.prepare_admin_operation(
                db,
                operation_type="admin-gallerydl-connectivity-test",
                scope_key="diagnostics:gallerydl:pixiv",
                title="Test Pixiv connectivity",
                entity="gallerydl-connectivity",
                options={"source": "pixiv"},
            )
            await TaskService(db).update_task(
                failed.task,
                status="failed",
                progress={"phase": "failed", "label": "Connection test failed"},
                error="subprocess unavailable",
            )
            await db.commit()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            pixiv = await client.get(
                "/api/v1/admin/gallerydl-config/test-connection/latest?source=pixiv",
                headers=_headers(f"{PREFIX}snapshot"),
            )
            assert pixiv.status_code == 200
            snapshot = pixiv.json()["snapshot"]
            assert snapshot["task_id"] == str(successful.task.id)
            assert snapshot["operation_type"] == "admin-gallerydl-connectivity-test"
            assert snapshot["status"] == "complete"
            assert snapshot["result"] == {
                "source": "pixiv",
                "success": True,
                "message": "usable snapshot",
            }
            assert datetime.fromisoformat(
                snapshot["completed_at"].replace("Z", "+00:00")
            ) == old_finished

            twitter = await client.get(
                "/api/v1/admin/gallerydl-config/test-connection/latest?source=twitter",
                headers=_headers(f"{PREFIX}snapshot"),
            )
            assert twitter.status_code == 200
            assert twitter.json() == {"snapshot": None}
    finally:
        async with async_session() as db:
            await _clear_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_backup_estimate_retries_same_task_and_publishes_snapshot(monkeypatch):
    """A worker exception is structured on TaskRun and retry advances its attempt."""
    from app.api.admin import backup
    from app.database import async_session, engine
    from app.jobs.admin_operations import _run_registered_admin_operation
    from app.main import app
    from app.models import TaskRun

    _stub_rq_transport(monkeypatch)
    transport = ASGITransport(app=app)
    task_id: UUID | None = None
    try:
        async with async_session() as db:
            await _clear_rows(db)
            await _seed_user(db, f"{PREFIX}retry", permissions=["system"])

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            started = await client.post(
                "/api/v1/admin/backup/estimate",
                headers=_headers(f"{PREFIX}retry"),
            )
            assert started.status_code == 202
            task_id = UUID(started.json()["task_id"])

            monkeypatch.setattr(
                backup,
                "_estimate_component_sizes",
                lambda: (_ for _ in ()).throw(RuntimeError("estimate unavailable")),
            )
            with pytest.raises(RuntimeError, match="estimate unavailable"):
                await _run_registered_admin_operation(str(task_id), 1)

            async with async_session() as db:
                failed = await db.get(TaskRun, task_id)
                assert failed.status == "failed"
                assert failed.progress_data == {
                    "phase": "failed",
                    "label": "Operation failed",
                }
                assert failed.error_log == "estimate unavailable"
                assert failed.attempts == 1

            failure_status = await client.get(
                f"/api/v1/admin/operations/{task_id}",
                headers=_headers(f"{PREFIX}retry"),
            )
            assert failure_status.status_code == 200
            assert failure_status.json()["error"] == "estimate unavailable"
            assert failure_status.json()["reason_code"] == "task_failed"

            retried = await client.post(
                f"/api/v1/admin/operations/{task_id}/retry",
                headers=_headers(f"{PREFIX}retry"),
            )
            assert retried.status_code == 202
            assert retried.json() == {
                "task_id": str(task_id),
                "job_id": f"admin-{task_id}-attempt-2",
                "status": "enqueued",
                "operation_type": "admin-backup-estimate",
            }

            monkeypatch.setattr(
                backup,
                "_estimate_component_sizes",
                lambda: {"database": 2048, "gallerydl-config": 1024},
            )
            result = await _run_registered_admin_operation(str(task_id), 2)
            assert result == {
                "components": {"database": 2.0, "gallerydl-config": 1.0},
                "message": "Backup estimate complete",
            }

            latest = await client.get(
                "/api/v1/admin/backup/estimate/latest",
                headers=_headers(f"{PREFIX}retry"),
            )
            assert latest.status_code == 200
            assert latest.json()["snapshot"]["task_id"] == str(task_id)
            assert latest.json()["snapshot"]["result"] == result
    finally:
        async with async_session() as db:
            await _clear_rows(db)
        await engine.dispose()
