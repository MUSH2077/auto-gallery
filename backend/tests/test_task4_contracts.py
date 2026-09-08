"""Real HTTP/PostgreSQL contracts for task policy and complete bounded reads."""

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def db():
    from app.database import async_session, engine

    async with async_session() as session:
        await session.execute(text("TRUNCATE download_repeat_intents, task_runs, creators, users, tags, works RESTART IDENTITY CASCADE"))
        await session.commit()
        yield session
        await session.rollback()
    await engine.dispose()


@pytest.fixture
async def client(db):
    from app.models import User
    from app.auth import create_access_token
    from app.main import app

    user = User(username=f"contracts-{uuid4()}", password_hash="test", is_admin=True, is_active=True, must_change_password=False)
    db.add(user)
    await db.commit()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {create_access_token(user.username, must_change_password=False)}"},
    ) as http:
        http.actor_id = user.id
        yield http


async def test_task_operation_type_is_exact_with_independent_text_and_status(db, client):
    from app.models import TaskRun

    ids = [uuid4(), uuid4(), uuid4()]
    for task_id, operation, status in zip(
        ids, ["subscription-sync-batch", "subscription-sync-batch-cleanup", "subscription-sync-batch"], ["running", "running", "failed"]
    ):
        db.add(
            TaskRun(
                id=task_id, kind="admin", operation_type=operation, status=status, owner_user_id=client.actor_id, title="Needle subscription-sync-batch-cleanup"
            )
        )
    db.add(TaskRun(kind="admin", operation_type="subscription-sync-batch", status="failed", owner_user_id=client.actor_id, title="Unrelated title"))
    await db.commit()
    for include in [False, True]:
        r = await client.get("/api/v1/tasks", params={"operation_type": "subscription-sync-batch", "q": "Needle", "include_account": str(include).lower()})
        assert r.status_code == 200, r.text
        assert {row["id"] for row in r.json()["items"]} == {str(ids[0]), str(ids[2])}
    r = await client.get("/api/v1/tasks", params={"operation_type": "subscription-sync-batch", "status": "failed", "q": "Needle"})
    assert [row["id"] for row in r.json()["items"]] == [str(ids[2])]


async def test_tags_pages_are_complete_bounded_and_count_queries_are_grouped(db, client):
    from app.models import Tag, Work, WorkTag, WorkSource, WorkSourceTag
    from sqlalchemy import event
    from app.database import engine

    tags = [Tag(normalized_name=f"tag-{i:04d}", category="general") for i in range(601)]
    db.add_all(tags)
    works = [Work(title=f"work {i}") for i in range(200)]
    db.add_all(works)
    await db.flush()
    db.add_all([WorkTag(work_id=work.id, tag_id=tag.id) for tag in tags[:10] for work in works])
    sources = [WorkSource(work_id=works[0].id, source="pixiv", source_work_id=f"multi-{i}") for i in range(2)]
    db.add_all(sources)
    await db.flush()
    db.add_all([WorkSourceTag(work_source_id=source.id, tag_id=tags[0].id, source="pixiv", original_name=tags[0].normalized_name) for source in sources])
    await db.commit()
    statements = []
    plans = []
    measurements = []

    def observed(_conn, _cursor, statement, _params, _context, _many):
        if "work_tags" in statement.lower():
            statements.append(statement)
            plans.append((statement, _params))

    event.listen(engine.sync_engine, "before_cursor_execute", observed)
    try:
        seen = []
        for offset in range(0, 601, 200):
            import time

            started = time.perf_counter()
            r = await client.get("/api/v1/tags/page", params={"offset": offset, "limit": 200, "sort_by": "name", "sort_order": "asc"})
            assert r.status_code == 200, r.text
            data = r.json()
            measurements.append({"offset": offset, "rows": len(data["items"]), "bytes": len(r.content), "elapsed_seconds": time.perf_counter() - started})
            assert data["total"] == 601 and len(data["items"]) <= 200
            assert data["next_offset"] == (offset + len(data["items"]) if offset + len(data["items"]) < 601 else None)
            seen.extend(row["id"] for row in data["items"])
        assert len(seen) == len(set(seen)) == 601
        assert len(statements) == 4, "One grouped direct-usage query per name page, not per tag"
        assert all("GROUP BY" in stmt for stmt in statements)
        r = await client.get("/api/v1/tags/page", params={"q": "tag-0600"})
        assert r.json()["total"] == 1 and r.json()["items"][0]["usage_count"] == 0
        r = await client.get("/api/v1/tags/page", params={"limit": 201})
        assert r.status_code == 422
        r = await client.get("/api/v1/tags/page", params={"sort_by": "usage_count", "limit": 5})
        assert [row["usage_count"] for row in r.json()["items"]] == [200] * 5
        first = await client.get("/api/v1/tags/page", params={"q": "tag-0000", "category": "general"})
        assert first.json()["items"][0]["source_usage"] == [{"source": "pixiv", "work_count": 1}]
        assert first.json()["items"][0]["usage_count"] == 200
        assert (await client.get("/api/v1/tags", params={"limit": 2})).status_code == 200
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observed)
    import json
    from pathlib import Path

    conn = await db.connection()
    explained = []
    for statement, params in [plans[0], next(pair for pair in plans if "LEFT OUTER JOIN" in pair[0] and "WHERE" not in pair[0])]:
        plan = (await conn.exec_driver_sql("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, params)).scalar_one()
        assert "SubPlan" not in json.dumps(plan), "New tag pages must not execute per-tag correlated subplans"
        explained.append({"sql": statement, "plan": plan})
    Path("/evidence/backend-task4/tag-query-plans.json").write_text(json.dumps(explained, indent=2))
    Path("/evidence/backend-task4/tag-http-bounds.json").write_text(json.dumps(measurements, indent=2))


async def test_scheduler_decision_pages_reach_tail_and_search_literal_before_limit(db, client):
    from tests.test_scheduler_batches import seed
    from app.models import SubscriptionSource

    await seed(db, number=809)
    sources = list((await db.execute(select(SubscriptionSource).order_by(SubscriptionSource.source_creator_id))).scalars())
    sources[-1].source_url = "https://www.pixiv.net/users/9999?needle=%_"
    tail_id = sources[-1].id
    for source in sources[-10:]:
        source.auth_healthy = False
    sources[0].is_enabled = False
    await db.commit()
    seen = []
    all_items = []
    measurements = []
    for offset in range(0, 809, 200):
        import time

        started = time.perf_counter()
        r = await client.get("/api/v1/system/scheduler-decisions", params={"offset": offset, "limit": 200})
        assert r.status_code == 200, r.text
        payload = r.json()
        measurements.append({"offset": offset, "rows": len(payload["items"]), "bytes": len(r.content), "elapsed_seconds": time.perf_counter() - started})
        assert payload["total"] == 809
        assert "next_offset" in payload and "summary" in payload
        seen.extend(row["source_id"] for row in payload["items"])
        all_items.extend(payload["items"])
    assert len(seen) == len(set(seen)) == 809
    r = await client.get("/api/v1/system/scheduler-decisions", params={"q": "%_", "limit": 1})
    assert r.json()["total"] == 1 and r.json()["items"][0]["source_id"] == str(tail_id)
    r = await client.get("/api/v1/system/scheduler-decisions", params={"q": "no-such-plan", "state": "due"})
    assert r.json()["total"] == 0 and r.json()["items"] == []

    expected = {row["source_id"] for row in all_items if row["is_attention"]}
    attention = []
    for offset in range(0, len(expected), 3):
        payload = (await client.get("/api/v1/system/scheduler-decisions", params={"view": "attention", "offset": offset, "limit": 3})).json()
        assert payload["total"] == len(expected) and payload["summary"]["blocked_count"] == 10
        attention.extend(row["source_id"] for row in payload["items"])
    assert set(attention) == expected
    disabled = (await client.get("/api/v1/system/scheduler-decisions", params={"state": "disabled"})).json()
    assert disabled["total"] == 1
    due = (await client.get("/api/v1/system/scheduler-decisions", params={"state": "due", "offset": 500, "limit": 100})).json()
    assert due["total"] == sum(row["due"] for row in all_items)
    import json
    from pathlib import Path

    Path("/evidence/backend-task4/scheduler-http-bounds.json").write_text(json.dumps(measurements, indent=2))


async def owned_download(db, actor_id, status="complete"):
    from tests.test_scheduler_batches import seed
    from app.models import DownloadJob, SubscriptionSource, UserSubscription, UserSubscriptionSource
    from app.services.tasks import TaskService

    ids = await seed(db)
    source = await db.get(SubscriptionSource, ids[0])
    membership = UserSubscription(user_id=actor_id, subscription_id=source.subscription_id, is_active=True, sync_enabled=True, schedule_mode="interval")
    db.add(membership)
    await db.flush()
    binding = UserSubscriptionSource(
        user_id=actor_id,
        subscription_id=source.subscription_id,
        user_subscription_id=membership.id,
        subscription_source_id=source.id,
        is_enabled=True,
        auth_healthy=True,
    )
    db.add(binding)
    job = DownloadJob(
        subscription_id=source.subscription_id,
        subscription_source_id=source.id,
        owner_user_id=actor_id,
        triggering_user_subscription_id=membership.id,
        source="pixiv",
        source_url=source.source_url,
        status=status,
    )
    db.add(job)
    await db.flush()
    task = await TaskService(db).ensure_download_task(job)
    await db.commit()
    return job.id, task.id


async def test_domain_and_unified_actions_match_and_completed_import_never_retries(db, client):
    from app.models import ImportJob
    from app.services.tasks import TaskService

    job_id, task_id = await owned_download(db, client.actor_id)
    job = (await client.get(f"/api/v1/download-jobs/{job_id}")).json()
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert job["available_actions"] == task["available_actions"]
    assert "retry" not in task["available_actions"] and "repeat_sync" in task["available_actions"]
    for url in [f"/api/v1/tasks/{task_id}/retry", f"/api/v1/download-jobs/{job_id}/retry"]:
        response = await client.post(url)
        assert response.status_code == 409
        assert response.json()["detail"]["reason"] == "completed_sync_requires_repeat"
    child = ImportJob(download_job_id=job_id, status="complete")
    db.add(child)
    await db.flush()
    child_task = await TaskService(db).ensure_import_task(child)
    child_id, child_task_id = child.id, child_task.id
    await db.commit()
    for url in [f"/api/v1/tasks/{child_task_id}/retry", f"/api/v1/import-jobs/{child_id}/retry"]:
        assert (await client.post(url)).status_code == 409
    await db.rollback()
    assert (await db.get(ImportJob, child_id, populate_existing=True)).status == "complete"


async def test_repeat_sync_binds_request_identity_and_preserves_original_receipt(db, client):
    from app.models import DownloadJob, RepositorySyncReceipt, TaskRun
    from app.services.operation_attention import upsert_repository_sync_receipt

    job_id, task_id = await owned_download(db, client.actor_id)
    old = await db.get(DownloadJob, job_id)
    await upsert_repository_sync_receipt(db, old, status="complete")
    await db.commit()
    request_id = str(uuid4())
    response = await client.post(f"/api/v1/tasks/{task_id}/repeat-sync", json={"request_id": request_id})
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["previous_job_id"] == str(job_id) and accepted["job_id"] != str(job_id)
    assert accepted["action"] == "repeat_sync"
    replay = await client.post(f"/api/v1/download-jobs/{job_id}/repeat-sync", json={"request_id": request_id})
    assert replay.status_code == 202 and replay.json()["job_id"] == accepted["job_id"]
    await db.rollback()
    new_id = __import__("uuid").UUID(accepted["job_id"])
    new = await db.get(DownloadJob, new_id)
    from app.services.download_finalization import finalize_download_job

    await finalize_download_job(db, new, status="failed", error="new repeat failed")
    await db.commit()
    receipt = (await db.execute(select(RepositorySyncReceipt).where(RepositorySyncReceipt.source_download_job_id == job_id))).scalar_one()
    assert receipt.status == "complete"
    assert (await db.get(DownloadJob, job_id, populate_existing=True)).status == "complete"
    new_task = await db.get(TaskRun, __import__("uuid").UUID(accepted["task_id"]))
    await db.delete(new_task)
    await db.delete(new)
    await db.commit()
    replay = await client.post(f"/api/v1/download-jobs/{job_id}/repeat-sync", json={"request_id": request_id})
    assert replay.status_code == 202 and replay.json()["job_id"] == accepted["job_id"]
    assert len(list((await db.execute(select(DownloadJob))).scalars())) == 1


async def test_active_delete_and_removed_membership_repeat_are_refused(db, client):
    from app.models import DownloadJob, UserSubscription

    job_id, task_id = await owned_download(db, client.actor_id, "downloading")
    for url in [f"/api/v1/download-jobs/{job_id}", f"/api/v1/tasks/{task_id}"]:
        response = await client.delete(url)
        assert response.status_code == 409, response.text
    await db.rollback()
    job = await db.get(DownloadJob, job_id)
    job.status = "complete"
    member = (await db.execute(select(UserSubscription).where(UserSubscription.user_id == client.actor_id))).scalar_one()
    member.is_active = False
    await db.commit()
    response = await client.post(f"/api/v1/download-jobs/{job_id}/repeat-sync", json={"request_id": str(uuid4())})
    assert response.status_code == 409, response.text
    assert len(list((await db.execute(select(DownloadJob))).scalars())) == 1


async def test_all_filtered_over_limit_refuses_before_any_mutation(db, client, monkeypatch):
    from app.api import download_jobs
    from app.models import DownloadJob

    job_id, _ = await owned_download(db, client.actor_id, "failed")
    original = await db.get(DownloadJob, job_id)
    db.add_all(
        [
            DownloadJob(
                subscription_id=original.subscription_id,
                subscription_source_id=original.subscription_source_id,
                owner_user_id=client.actor_id,
                source="pixiv",
                source_url=original.source_url,
                status="failed",
            )
            for _ in range(2)
        ]
    )
    await db.commit()
    monkeypatch.setattr(download_jobs, "BULK_ACTION_LIMIT", 2, raising=False)
    response = await client.post("/api/v1/download-jobs/batch-by-filter", json={"filters": {"status": "failed"}, "action": "delete"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "batch_limit_exceeded"
    assert response.json()["detail"]["total_matched"] == 3
    await db.rollback()
    assert len(list((await db.execute(select(DownloadJob))).scalars())) == 3


async def test_workbench_recent_and_registered_capabilities_respect_actor(db, client):
    from app.models import User, TaskRun
    from app.auth import create_access_token

    job_id, task_id = await owned_download(db, client.actor_id, "failed")
    response = await client.get("/api/v1/system/workbench", params={"refresh": "true"})
    assert response.status_code == 200, response.text
    recent = next(row for row in response.json()["recent"]["download_jobs"] if row["id"] == str(job_id))
    assert recent["available_actions"] == (await client.get(f"/api/v1/tasks/{task_id}")).json()["available_actions"]
    legacy = TaskRun(kind="admin", operation_type="legacy-rebuild", status="failed", title="Legacy", attention_state="open")
    db.add(legacy)
    await db.flush()
    legacy_id = legacy.id
    limited = User(username=f"system-only-{uuid4()}", password_hash="test", permissions=["system"], is_active=True, must_change_password=False)
    db.add(limited)
    await db.commit()
    response = await client.get(f"/api/v1/tasks/{legacy_id}")
    assert "retry" not in response.json()["available_actions"]
    assert (await client.post(f"/api/v1/tasks/{legacy_id}/retry")).json()["detail"]["reason"] == "legacy_operation_read_only"
    headers = {"Authorization": f"Bearer {create_access_token(limited.username, must_change_password=False)}"}
    assert (await client.get(f"/api/v1/tasks/{task_id}", headers=headers)).status_code == 403
    limited_response = await client.get("/api/v1/system/workbench", headers=headers)
    assert limited_response.json()["recent"]["download_jobs"] == [], "Cached global admin jobs must not cross actor visibility"


async def test_repeat_policy_rechecks_inactive_subscription_and_provider(db, client):
    from app.models import DownloadJob, Subscription

    job_id, _ = await owned_download(db, client.actor_id)
    job = await db.get(DownloadJob, job_id)
    sub = await db.get(Subscription, job.subscription_id)
    sub.is_active = False
    await db.commit()
    response = await client.get(f"/api/v1/download-jobs/{job_id}")
    assert "repeat_sync" not in response.json()["available_actions"]
    assert (await client.post(f"/api/v1/download-jobs/{job_id}/repeat-sync", json={"request_id": str(uuid4())})).status_code == 409


async def test_control_rechecks_completion_committed_after_initial_policy(db, client, monkeypatch):
    from app.models import DownloadJob
    from app.services.task_engine import TaskEngine
    from app.services.download_finalization import finalize_download_job
    from app.database import async_session

    job_id, task_id = await owned_download(db, client.actor_id, "importing")
    original = TaskEngine._get_download

    async def complete_before_lock(engine, identity):
        job = await original(engine, identity)
        # Another real transaction commits after the endpoint's advertised
        # policy/load and before mutation's authoritative lock.
        async with async_session() as other:
            current = await other.get(DownloadJob, identity)
            await finalize_download_job(other, current, status="complete")
            await other.commit()
        return job

    monkeypatch.setattr(TaskEngine, "_get_download", complete_before_lock)
    response = await client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert response.status_code == 409, response.text
    await db.rollback()
    assert (await db.get(DownloadJob, job_id, populate_existing=True)).status == "complete"


async def test_terminal_delete_waits_for_child_settlement_and_preserves_receipt(db, client):
    from app.models import ImportJob, DownloadJob, RepositorySyncReceipt
    from app.services.tasks import TaskService
    from app.services.redis_client import get_redis

    job_id, task_id = await owned_download(db, client.actor_id)
    child = ImportJob(download_job_id=job_id, status="cancelled", execution_token=uuid4())
    db.add(child)
    await db.flush()
    await TaskService(db).ensure_import_task(child)
    child_id = child.id
    await db.commit()
    response = await client.delete(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 409 and response.json()["detail"]["reason"] == "active_import"
    child.execution_token = None
    await db.commit()
    redis = get_redis()
    redis.set(f"task:{child_id}:heartbeat_ts", "1", ex=30)
    try:
        response = await client.delete(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 409 and response.json()["detail"]["reason"] == "execution_unsettled"
    finally:
        redis.delete(f"task:{child_id}:heartbeat_ts")
    response = await client.delete(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 200, response.text
    await db.rollback()
    assert await db.get(DownloadJob, job_id, populate_existing=True) is None
    receipt = (await db.execute(select(RepositorySyncReceipt).where(RepositorySyncReceipt.source_download_job_id == job_id))).scalar_one()
    assert receipt.status == "complete"


async def test_action_authorization_holds_domain_lock_until_control_commits(db, client):
    import asyncio
    from app.database import async_session
    from app.models import DownloadJob
    from app.services.task_actions import require_job_action

    job_id, _ = await owned_download(db, client.actor_id, "downloading")
    async with async_session() as control, async_session() as completion:
        job = await control.get(DownloadJob, job_id)
        await require_job_action(control, job, "download", "cancel")
        await completion.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(Exception, match="LockNotAvailable|lock timeout"):
            await asyncio.wait_for(completion.execute(select(DownloadJob).where(DownloadJob.id == job_id).with_for_update()), timeout=2)
        await completion.rollback()
        await control.rollback()
        assert (await completion.execute(select(DownloadJob).where(DownloadJob.id == job_id).with_for_update())).scalar_one().status == "downloading"


async def test_repeat_concurrent_intents_and_transport_failure_keep_durable_identity(db, client, monkeypatch):
    import asyncio
    from app.models import DownloadRepeatIntent, DownloadJob, TaskRun, User
    from app.auth import create_access_token
    from app.services import download_dispatch

    job_id, task_id = await owned_download(db, client.actor_id)

    async def unavailable(*args, **kwargs):
        raise ConnectionError("Injected transport loss after durable acceptance")

    monkeypatch.setattr(download_dispatch, "recover_download_dispatch_candidate", unavailable)
    request_id = str(uuid4())
    responses = await asyncio.gather(*[client.post(f"/api/v1/download-jobs/{job_id}/repeat-sync", json={"request_id": request_id}) for _ in range(2)])
    assert [r.status_code for r in responses] == [202, 202], [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    accepted = responses[0].json()
    await db.rollback()
    assert len(list((await db.execute(select(DownloadRepeatIntent))).scalars())) == 1
    child = await db.get(TaskRun, __import__("uuid").UUID(accepted["task_id"]))
    assert child.status == "enqueued" and child.meta["download_dispatch"]["state"] == "pending"
    conflict = await client.post(f"/api/v1/tasks/{task_id}/repeat-sync", json={"request_id": str(uuid4())})
    assert conflict.status_code == 409 and conflict.json()["detail"]["existing_job_id"] == accepted["job_id"], conflict.text
    other = User(username=f"outsider-{uuid4()}", password_hash="test", permissions=["tasks"], is_active=True, must_change_password=False)
    db.add(other)
    await db.commit()
    headers = {"Authorization": f"Bearer {create_access_token(other.username, must_change_password=False)}"}
    assert (await client.post(f"/api/v1/download-jobs/{job_id}/repeat-sync", headers=headers, json={"request_id": request_id})).status_code == 404
    assert len(list((await db.execute(select(DownloadJob))).scalars())) == 2


async def test_explicit_empty_batch_and_foreign_ids_never_expand_scope(db, client):
    from app.models import DownloadJob

    job_id, _ = await owned_download(db, client.actor_id, "failed")
    for filters in [{"ids": []}, {"ids": [str(uuid4())]}]:
        response = await client.post("/api/v1/download-jobs/batch-by-filter", json={"filters": filters, "action": "delete"})
        assert response.status_code == 200 and response.json()["total_matched"] == 0, response.text
    assert await db.get(DownloadJob, job_id, populate_existing=True) is not None


async def test_fast_retry_worker_terminal_truth_survives_unified_response(db, client, monkeypatch):
    from app.services import task_engine
    from app.database import async_session
    from app.models import DownloadJob, TaskRun
    from app.services.download_finalization import finalize_download_job

    job_id, task_id = await owned_download(db, client.actor_id, "failed")
    original = task_engine.publish_prepared_download

    async def published_and_finished(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with async_session() as worker:
            job = await worker.get(DownloadJob, job_id)
            await finalize_download_job(worker, job, status="failed", error="Fast worker result")
        return result

    monkeypatch.setattr(task_engine, "publish_prepared_download", published_and_finished)
    response = await client.post(f"/api/v1/tasks/{task_id}/retry")
    assert response.status_code == 200, response.text
    await db.rollback()
    assert (await db.get(DownloadJob, job_id, populate_existing=True)).status == "failed"
    assert (await db.get(TaskRun, task_id, populate_existing=True)).status == "failed"


async def test_registered_admin_policy_matches_real_retry_and_module_permissions(db, client):
    from app.services.operations import prepare_admin_operation
    from app.models import User
    from app.auth import create_access_token

    prepared = await prepare_admin_operation(
        db, operation_type="admin-search-reindex", scope_key="library:search-reindex:active", title="Search rebuild", entity="search", options={}
    )
    prepared.task.status = "failed"
    task_id = prepared.task.id
    await db.commit()
    response = await client.get(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 200 and "retry" in response.json()["available_actions"], response.text
    limited = User(username=f"tasks-only-{uuid4()}", password_hash="test", permissions=["tasks"], is_active=True, must_change_password=False)
    db.add(limited)
    await db.commit()
    headers = {"Authorization": f"Bearer {create_access_token(limited.username, must_change_password=False)}"}
    for method, path in [("GET", f"/api/v1/tasks/{task_id}"), ("POST", f"/api/v1/tasks/{task_id}/retry")]:
        assert (await client.request(method, path, headers=headers)).status_code == 403
    response = await client.post(f"/api/v1/tasks/{task_id}/retry")
    assert response.status_code == 200, response.text
    response = await client.get(f"/api/v1/tasks/{task_id}")
    assert "retry" not in response.json()["available_actions"]


async def test_openapi_exports_capabilities_and_bounded_contracts(client):
    schema = (await client.get("/api/openapi.json")).json()
    components = schema["components"]["schemas"]
    for name in ["TaskRead", "DownloadJobRead", "ImportJobRead", "WorkbenchRecentJob"]:
        assert "available_actions" in components[name]["properties"]
        assert "disabled_reasons" in components[name]["properties"]
    assert components["RepeatSyncRequest"]["required"] == ["request_id"]
    for name in ["TagPage", "SchedulerDecisionPage"]:
        assert {"items", "total", "offset", "limit", "next_offset"} <= set(components[name]["properties"])


async def test_repeat_migration_applies_and_reverses_on_real_postgres(db):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[1] / "alembic/versions/fb13c5d7e9a1_download_repeat_intents.py"
    spec = importlib.util.spec_from_file_location("repeat_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    connection = await db.connection()

    def exercise(sync_conn):
        from sqlalchemy import inspect

        sync_conn.exec_driver_sql("DROP TABLE download_repeat_intents")
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()
            inspector = inspect(sync_conn)
            assert {c["name"] for c in inspector.get_columns("download_repeat_intents")} >= {
                "actor_user_id",
                "request_id",
                "previous_job_id",
                "previous_task_id",
                "download_job_id",
                "task_id",
            }
            assert any(c["column_names"] == ["actor_user_id", "request_id"] for c in inspector.get_unique_constraints("download_repeat_intents"))
            migration.downgrade()
            assert not inspect(sync_conn).has_table("download_repeat_intents")

    try:
        await connection.run_sync(exercise)
    finally:
        await db.rollback()


async def test_bulk_partial_refusal_does_not_rollback_prior_success(db, client):
    from app.models import DownloadJob

    active_id, active_task = await owned_download(db, client.actor_id, "enqueued")
    complete_id, _ = await owned_download(db, client.actor_id, "complete")
    # Force a deterministic successful-then-refused selection order using the
    # actual engine, since API selection itself orders UUIDs independently.
    from app.services.task_engine import TaskEngine

    engine = TaskEngine(db)
    result = await engine.batch_by_filter("download", {"ids": [str(active_id), str(complete_id)]}, "pause")
    assert result["succeeded"] == 1 and result["failed"] == 1
    await db.rollback()
    assert (await db.get(DownloadJob, active_id, populate_existing=True)).status == "paused"
    # The remaining controls run through real unified routes and dispatch.
    response = await client.post(f"/api/v1/tasks/{active_task}/resume")
    assert response.status_code == 200, response.text
    cleared = await client.post("/api/v1/download-jobs/clear", json={"statuses": ["enqueued"]})
    assert cleared.status_code == 200 and cleared.json()["deleted"] == 0
    assert cleared.json()["failed"] == 1 and cleared.json()["errors"][0]["error"]["reason"] == "active_work_cancel_first"
    response = await client.post(f"/api/v1/tasks/{active_task}/cancel")
    assert response.status_code == 200, response.text
    assert (await client.post(f"/api/v1/tasks/{active_task}/retry")).status_code == 409


async def test_delegated_import_controls_and_attention_use_shared_policy(db, client):
    from app.models import ImportJob, TaskRun
    from app.services.tasks import TaskService

    job_id, task_id = await owned_download(db, client.actor_id, "importing")
    child = ImportJob(download_job_id=job_id, status="running")
    db.add(child)
    await db.flush()
    child_task = await TaskService(db).ensure_import_task(child, parent_task_id=task_id)
    child_id, child_task_id = child.id, child_task.id
    await db.commit()
    response = await client.post(f"/api/v1/tasks/{task_id}/pause")
    assert response.status_code == 200 and response.json()["delegated_to"] == str(child_id), response.text
    detail = (await client.get(f"/api/v1/import-jobs/{child_id}")).json()
    assert "resume" in detail["available_actions"] and "retry" not in detail["available_actions"]
    response = await client.post(f"/api/v1/tasks/{task_id}/cancel")
    assert response.status_code == 200, response.text
    await db.rollback()
    child_task = await db.get(TaskRun, child_task_id)
    child_task.attention_state = "open"
    await db.commit()
    response = await client.get("/api/v1/tasks/anomalies", params={"view": "attention"})
    assert response.status_code == 200, response.text
    anomaly = next(row for row in response.json()["items"] if row["task_id"] == str(child_task_id))
    assert "retry" not in anomaly["available_actions"] and "acknowledge" in anomaly["available_actions"]
    assert "copy_diagnostics" in anomaly["navigation_actions"]


async def test_download_capabilities_keep_live_progress_reads_side_effect_free(db, client):
    from app.services.download import DownloadService
    from app.services.progress import ProgressTracker
    from app.models import DownloadJob

    job_id, _ = await owned_download(db, client.actor_id, "downloading")
    ProgressTracker.set(str(job_id), {"current": 7, "total": 9, "label": "Live snapshot"})
    job = await DownloadService(db).get_job(job_id, user_id=client.actor_id)
    assert job.progress_data["current"] == 7
    assert not db.is_modified(job), "Read-only progress cannot dirty the mapped domain row before capability queries/control"
    await db.commit()
    await db.refresh(job)
    assert not job.progress_data or job.progress_data.get("current") != 7


async def test_direct_admin_surface_keeps_own_module_permissions_without_private_leak(db, client):
    from app.services.operations import prepare_admin_operation
    from app.models import User
    from app.auth import create_access_token

    prepared = await prepare_admin_operation(
        db, operation_type="admin-search-reindex", scope_key="library:search-reindex:active", title="Search rebuild", entity="search", options={}
    )
    prepared.task.status = "failed"
    task_id = prepared.task.id
    system_user = User(username=f"system-{uuid4()}", password_hash="test", permissions=["system"], is_active=True, must_change_password=False)
    db.add(system_user)
    await db.commit()
    headers = {"Authorization": f"Bearer {create_access_token(system_user.username, must_change_password=False)}"}
    response = await client.get(f"/api/v1/admin/operations/{task_id}", headers=headers)
    assert response.status_code == 200 and "retry" in response.json()["available_actions"], response.text
    response = await client.post(f"/api/v1/admin/operations/{task_id}/retry", headers=headers)
    assert response.status_code == 202, response.text
    # A registered operation in another owning module is not a system grant.
    protected = await prepare_admin_operation(
        db,
        operation_type="admin-danbooru-url-batch-import",
        scope_key="danbooru:url-batch-import:test",
        title="Private import",
        entity="urls",
        options={},
        queue_name="imports",
    )
    protected_id = protected.task.id
    await db.commit()
    assert (await client.get(f"/api/v1/admin/operations/{protected_id}", headers=headers)).status_code == 403


async def test_malformed_registered_dispatch_is_read_only_not_a_serializer_error(db, client):
    from app.models import TaskRun
    task_id = uuid4()
    task = TaskRun(id=task_id, kind="admin", operation_type="asset-dedup-scan", status="failed", attempts=1,
                   rq_job_id=f"admin-{task_id}-attempt-1", meta={"admin_dispatch": {"operation_type": "asset-dedup-scan", "scope_key": 123,
                   "queue_name": "maintenance", "attempt": 1, "rq_job_id": f"admin-{task_id}-attempt-1", "options": []}})
    db.add(task)
    await db.commit()
    response = await client.get(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 200 and "retry" not in response.json()["available_actions"], response.text
    response = await client.post(f"/api/v1/tasks/{task_id}/retry")
    assert response.status_code == 409, response.text


async def test_history_delete_keeps_existing_receipt_statistics_after_detail_compaction(db, client):
    from app.models import DownloadJob, RepositorySyncReceipt
    from app.services.operation_attention import upsert_repository_sync_receipt
    job_id, _ = await owned_download(db, client.actor_id)
    job = await db.get(DownloadJob, job_id)
    job.manifest = {"import_stats": {"works": 7}, "image_count": 4}
    await db.flush()
    await db.refresh(job)
    receipt = await upsert_repository_sync_receipt(db, job)
    await db.commit()
    receipt_id, finished_at = receipt.id, receipt.finished_at
    job.manifest = {}
    await db.commit()
    response = await client.delete(f"/api/v1/download-jobs/{job_id}")
    assert response.status_code == 200, response.text
    await db.rollback()
    receipt = await db.get(RepositorySyncReceipt, receipt_id, populate_existing=True)
    assert receipt.works_imported == 7 and receipt.media_count == 4
    assert receipt.finished_at == finished_at


async def test_visible_orphan_can_acknowledge_without_inventing_execution_actions(db, client):
    from app.models import TaskRun
    task = TaskRun(kind="download", subject_type="download_job", subject_id=uuid4(), owner_user_id=client.actor_id,
                   status="failed", attention_state="open", reason_code="orphaned_subject")
    db.add(task)
    await db.commit()
    task_id = task.id
    response = await client.get(f"/api/v1/tasks/{task_id}")
    assert response.status_code == 200 and response.json()["available_actions"] == ["acknowledge"], response.text
    assert (await client.post(f"/api/v1/tasks/{task_id}/acknowledge")).status_code == 200
    assert (await client.get(f"/api/v1/tasks/{task_id}")).json()["attention_state"] == "acknowledged"
