"""Authorized task-list scopes and summary contracts."""
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

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
    from tests.conftest import access_token_for_user
    from app.main import app

    user = User(username=f"contracts-{uuid4()}", password_hash="test", is_admin=True, is_active=True, must_change_password=False)
    db.add(user)
    await db.commit()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {await access_token_for_user(user.username)}"},
    ) as http:
        http.actor_id = user.id
        yield http


async def test_task_scope_summary_is_authorized_before_scope_and_page(db, client):
    from app.models import TaskRun

    rows = [
        TaskRun(kind="admin", status="failed", attention_state="open", title="scope failed", owner_user_id=client.actor_id),
        TaskRun(kind="admin", status="stale", attention_state="acknowledged", title="scope stale", owner_user_id=client.actor_id),
        TaskRun(kind="admin", status="running", title="scope running", owner_user_id=client.actor_id),
        TaskRun(kind="admin", status="enqueued", resource_state="waiting", resource_reason="resource_pressure", title="scope waiting", owner_user_id=client.actor_id),
        TaskRun(kind="admin", status="complete", title="scope complete", owner_user_id=client.actor_id),
    ]
    db.add_all(rows)
    await db.commit()

    response = await client.get("/api/v1/tasks", params={"q": "scope", "scope": "running", "include_summary": "true", "offset": 0, "limit": 1})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["items"]) == 1
    assert payload["items"][0]["status"] == "running"
    assert payload["total"] == 1
    assert payload["summary"] == {"all": 5, "attention": 1, "running": 1, "waiting": 1}

    waiting = await client.get("/api/v1/tasks", params={"q": "scope", "scope": "waiting", "include_summary": "true"})
    assert waiting.status_code == 200, waiting.text
    assert [item["status"] for item in waiting.json()["items"]] == ["enqueued"]
    assert waiting.json()["summary"] == {"all": 5, "attention": 1, "running": 1, "waiting": 1}


async def test_task_list_scope_acl_system_only_and_legacy_include_account(db, client):
    from app.models import TaskRun, User
    from tests.conftest import access_token_for_user

    owner = User(
        username=f"task-owner-{uuid4()}",
        password_hash="test",
        permissions=["tasks"],
        is_active=True,
        must_change_password=False,
    )
    outsider = User(
        username=f"task-outsider-{uuid4()}",
        password_hash="test",
        permissions=["tasks"],
        is_active=True,
        must_change_password=False,
    )
    system = User(
        username=f"task-system-{uuid4()}",
        password_hash="test",
        permissions=["system"],
        is_active=True,
        must_change_password=False,
    )
    db.add_all([owner, outsider, system])
    await db.flush()
    owned = TaskRun(kind="admin", status="failed", attention_state="open", title="owned private", owner_user_id=owner.id)
    foreign = TaskRun(kind="admin", status="failed", attention_state="open", title="foreign private", owner_user_id=outsider.id)
    global_batch = TaskRun(
        kind="admin",
        operation_type="subscription-sync-batch",
        status="failed",
        attention_state="open",
        title="global subscription batch",
        owner_user_id=None,
        triggering_user_subscription_id=None,
        triggering_remote_account_id=None,
        subject_type=None,
        meta={},
    )
    db.add_all([owned, foreign, global_batch])
    await db.commit()

    owner_headers = {"Authorization": f"Bearer {await access_token_for_user(owner.username)}"}
    response = await client.get(
        "/api/v1/tasks",
        params={"q": "private", "include_summary": "true"},
        headers=owner_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["title"] for item in payload["items"]] == ["owned private"]
    assert payload["summary"]["all"] == 1
    assert payload["summary"]["attention"] == 1

    system_headers = {"Authorization": f"Bearer {await access_token_for_user(system.username)}"}
    response = await client.get(
        "/api/v1/tasks",
        params={"include_summary": "true"},
        headers=system_headers,
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["title"] for item in payload["items"]] == ["global subscription batch"]
    assert payload["summary"] == {"all": 1, "attention": 1, "running": 0, "waiting": 0}

    legacy = await client.get("/api/v1/tasks", params={"include_account": "true"})
    assert legacy.status_code == 200, legacy.text
    assert legacy.json().get("summary") is None
    rejected = await client.get(
        "/api/v1/tasks",
        params={"include_account": "true", "scope": "running"},
    )
    assert rejected.status_code == 422, rejected.text
