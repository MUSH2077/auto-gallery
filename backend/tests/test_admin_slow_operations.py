"""Asynchronous administrator diagnostics and backup operation regressions."""

from __future__ import annotations

import asyncio
import gc
import logging
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


async def _seed_user(
    db,
    username: str,
    *,
    permissions: list[str],
    is_admin: bool = False,
) -> None:
    from app.auth import hash_password
    from app.models.user import User

    db.add(
        User(
            username=username,
            password_hash=hash_password("hunter22"),
            is_admin=is_admin,
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
            assert twitter.json() == {"snapshot": None, "current": None}
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_proxy_worker_redacts_credentials_before_logging_and_persisting(
    monkeypatch,
    caplog,
):
    """Proxy userinfo may configure urllib but must never enter TaskRun history."""
    from app.api.admin import settings as settings_api
    from app.database import async_session, engine
    from app.jobs.admin_operations import _run_registered_admin_operation
    from app.main import app
    from app.models import TaskRun

    _stub_rq_transport(monkeypatch)
    secret = "proxy-secret-password"

    async def credentialed_proxy(*_args, **_kwargs):
        return {
            "enabled": True,
            "http_proxy": f"http://alice:{secret}@proxy.example:7890",
            "https_proxy": f"https://bob:{secret}@secure-proxy.example:8443",
            "no_proxy": "",
            "ssl_verify": True,
        }

    class FakeSocket:
        def close(self):
            return None

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class FakeOpener:
        def open(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(settings_api, "_get_setting", credentialed_proxy)
    monkeypatch.setattr("socket.create_connection", lambda *_args, **_kwargs: FakeSocket())
    monkeypatch.setattr("urllib.request.build_opener", lambda *_args, **_kwargs: FakeOpener())
    caplog.set_level(logging.INFO, logger=settings_api.__name__)

    transport = ASGITransport(app=app)
    task_id: UUID | None = None
    try:
        async with async_session() as db:
            await _clear_rows(db)
            await _seed_user(db, f"{PREFIX}proxy_redaction", permissions=["system"])

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            started = await client.post(
                "/api/v1/admin/proxy/test",
                headers=_headers(f"{PREFIX}proxy_redaction"),
            )
            assert started.status_code == 202
            task_id = UUID(started.json()["task_id"])

        result = await _run_registered_admin_operation(str(task_id), 1)
        rendered = str(result)
        assert secret not in rendered
        assert "alice" not in rendered
        assert "bob" not in rendered
        assert result["proxy_config"] == {
            "http": "http://proxy.example:7890",
            "https": "https://secure-proxy.example:8443",
        }
        assert secret not in caplog.text
        assert "alice" not in caplog.text
        assert "bob" not in caplog.text

        async with async_session() as db:
            persisted = await db.get(TaskRun, task_id)
            assert persisted.status == "complete"
            assert secret not in str(persisted.result_data)
            assert persisted.result_data["proxy_config"] == result["proxy_config"]
    finally:
        async with async_session() as db:
            await _clear_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tasks_permission_cannot_read_list_or_retry_system_operation(monkeypatch):
    """A tasks-only user cannot cross into a system operation's durable data."""
    from app.database import async_session, engine
    from app.main import app
    from app.services import operations
    from app.services.search import SearchService
    from app.services.tasks import TaskService

    _stub_rq_transport(monkeypatch)

    async def empty_external_search(
        _self,
        _query,
        targets,
        _resolved,
        _offset,
        _limit,
        _force_sfw,
        **_ownership_filters,
    ):
        return {target: {"total": 0, "items": []} for target in targets}

    monkeypatch.setattr(SearchService, "_search_meili", empty_external_search)
    secret = "legacy-proxy-password"
    task_id: UUID | None = None
    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear_rows(db)
            await _seed_user(db, f"{PREFIX}tasks_only", permissions=["tasks"])
            await _seed_user(
                db,
                f"{PREFIX}mixed_tasks",
                permissions=["library", "tasks"],
            )
            await _seed_user(
                db,
                f"{PREFIX}system_tasks",
                permissions=["tasks", "system"],
            )
            await _seed_user(
                db,
                f"{PREFIX}admin_tasks",
                permissions=[],
                is_admin=True,
            )
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-proxy-test",
                scope_key="diagnostics:proxy:active",
                title="Proxy connectivity test",
                entity="proxy-test",
                options={"proxy_url": f"http://alice:{secret}@proxy.example:7890"},
            )
            await TaskService(db).update_task(
                prepared.task,
                status="failed",
                progress={"phase": "failed", "label": "Proxy test failed"},
                result={"proxy": f"http://alice:{secret}@proxy.example:7890"},
                error=f"Cannot connect through http://alice:{secret}@proxy.example:7890",
            )
            await TaskService(db).update_task(
                prepared.task,
                attention_state="acknowledged",
            )
            task_id = prepared.task.id
            await db.commit()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            tasks_headers = _headers(f"{PREFIX}tasks_only")
            detail = await client.get(f"/api/v1/tasks/{task_id}", headers=tasks_headers)
            assert detail.status_code == 403
            assert secret not in detail.text

            listed = await client.get(
                "/api/v1/tasks?include_account=true",
                headers=tasks_headers,
            )
            assert listed.status_code == 200
            assert str(task_id) not in listed.text
            assert secret not in listed.text

            searched = await client.get(
                "/api/v1/tasks?q=Proxy",
                headers=tasks_headers,
            )
            assert searched.status_code == 200
            assert str(task_id) not in searched.text
            assert secret not in searched.text

            scoped_search = await client.get(
                "/api/v1/search?scope=tasks&q=Proxy",
                headers=tasks_headers,
            )
            assert scoped_search.status_code == 200
            assert scoped_search.json()["groups"]["tasks"]["total"] == 0
            assert str(task_id) not in scoped_search.text
            assert secret not in scoped_search.text

            global_search = await client.get(
                "/api/v1/search?scope=global&q=Proxy",
                headers=_headers(f"{PREFIX}mixed_tasks"),
            )
            assert global_search.status_code == 200
            assert "tasks" not in global_search.json()["groups"]
            assert str(task_id) not in global_search.text
            assert secret not in global_search.text

            overview = await client.get(
                "/api/v1/operations/overview?view=resolved",
                headers=tasks_headers,
            )
            assert overview.status_code == 200
            assert str(task_id) not in overview.text
            assert secret not in overview.text

            denied_retry = await client.post(
                f"/api/v1/tasks/{task_id}/retry",
                headers=tasks_headers,
            )
            assert denied_retry.status_code == 403
            assert secret not in denied_retry.text

            privileged_detail = await client.get(
                f"/api/v1/tasks/{task_id}",
                headers=_headers(f"{PREFIX}system_tasks"),
            )
            assert privileged_detail.status_code == 200
            assert privileged_detail.json()["id"] == str(task_id)

            for username in (f"{PREFIX}system_tasks", f"{PREFIX}admin_tasks"):
                privileged_search = await client.get(
                    "/api/v1/search?scope=tasks&q=Proxy",
                    headers=_headers(username),
                )
                assert privileged_search.status_code == 200
                items = privileged_search.json()["groups"]["tasks"]["items"]
                assert [item["id"] for item in items] == [str(task_id)]
                assert secret in privileged_search.text
    finally:
        async with async_session() as db:
            await _clear_rows(db)
        await engine.dispose()


def test_admin_operation_registry_uses_the_owning_module_permission():
    """Generic task access follows the API module that starts each operation."""
    from app.services.operations import admin_operation_required_permission

    assert admin_operation_required_permission("admin-proxy-test") == "system"
    assert admin_operation_required_permission("danbooru-mapping-refresh") == "subscriptions"
    assert admin_operation_required_permission("admin-danbooru-batch-import") == "subscriptions"
    assert admin_operation_required_permission("danbooru-import-all") == "subscriptions"
    assert admin_operation_required_permission("admin-danbooru-url-batch-import") == "subscriptions"
    assert admin_operation_required_permission("admin-curation-backfill") == "curation"
    assert admin_operation_required_permission("admin-gitllery-verify") == "curation"


@pytest.mark.asyncio
async def test_integrity_scan_rolls_back_and_raises_an_essential_query_failure():
    """An aborted scan is never translated into an empty All Clear result."""
    from app.api.admin import settings as settings_api

    class BrokenSession:
        rolled_back = False

        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("integrity database unavailable")

        async def rollback(self):
            self.rolled_back = True

    db = BrokenSession()
    with pytest.raises(RuntimeError, match="integrity database unavailable"):
        await settings_api._run_integrity_check(db)
    assert db.rolled_back is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integrity_aborted_transaction_fails_task_and_remains_retryable(monkeypatch):
    """Worker failure recording uses a fresh transaction after the scan aborts."""
    from sqlalchemy import text as sql_text

    from app.api.admin import settings as settings_api
    from app.database import async_session, engine
    from app.jobs.admin_operations import _run_registered_admin_operation
    from app.main import app
    from app.models import TaskRun

    _stub_rq_transport(monkeypatch)

    async def abort_transaction(db):
        await db.execute(sql_text("SELECT * FROM task2_missing_integrity_table"))

    monkeypatch.setattr(settings_api, "_run_integrity_check", abort_transaction)
    task_id: UUID | None = None
    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear_rows(db)
            await _seed_user(db, f"{PREFIX}integrity_failure", permissions=["system"])

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            started = await client.post(
                "/api/v1/admin/integrity-check",
                headers=_headers(f"{PREFIX}integrity_failure"),
            )
            assert started.status_code == 202
            task_id = UUID(started.json()["task_id"])

            with pytest.raises(Exception, match="task2_missing_integrity_table"):
                await _run_registered_admin_operation(str(task_id), 1)

            async with async_session() as db:
                failed = await db.get(TaskRun, task_id)
                assert failed.status == "failed"
                assert failed.result_data in (None, {})
                assert "task2_missing_integrity_table" in failed.error_log

            retry = await client.post(
                f"/api/v1/admin/operations/{task_id}/retry",
                headers=_headers(f"{PREFIX}integrity_failure"),
            )
            assert retry.status_code == 202
            assert retry.json()["task_id"] == str(task_id)
    finally:
        async with async_session() as db:
            await _clear_rows(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_latest_endpoint_returns_database_backed_current_operation(monkeypatch):
    """A remounted client can attach to the active TaskRun before starting."""
    from app.database import async_session, engine
    from app.main import app
    from app.services import operations

    _stub_rq_transport(monkeypatch)
    task_id: UUID | None = None
    transport = ASGITransport(app=app)
    try:
        async with async_session() as db:
            await _clear_rows(db)
            await _seed_user(db, f"{PREFIX}current", permissions=["system"])
            prepared = await operations.prepare_admin_operation(
                db,
                operation_type="admin-integrity-scan",
                scope_key="diagnostics:integrity:active",
                title="Integrity scan",
                entity="integrity",
                options={},
            )
            task_id = prepared.task.id
            await db.commit()

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/admin/integrity-check/latest",
                headers=_headers(f"{PREFIX}current"),
            )
            assert response.status_code == 200
            assert response.json() == {
                "snapshot": None,
                "current": {
                    "task_id": str(task_id),
                    "job_id": f"admin-{task_id}-attempt-1",
                    "status": "enqueued",
                    "operation_type": "admin-integrity-scan",
                    "progress": {"phase": "enqueued", "label": "Integrity scan queued"},
                },
            }
    finally:
        async with async_session() as db:
            await _clear_rows(db)
        await engine.dispose()
