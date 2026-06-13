import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, *, first=None, scalars=None, rows=None):
        self._first = first
        self._scalars = scalars or []
        self._rows = rows or []

    def first(self):
        return self._first

    def scalars(self):
        return _ScalarResult(self._scalars)

    def __iter__(self):
        return iter(self._rows)


class _FakeDB:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return self.results.pop(0)


def _dt():
    return datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


def _source_context(source_id, sub_id, creator_id):
    ss = SimpleNamespace(
        id=source_id,
        subscription_id=sub_id,
        source="pixiv",
        source_creator_id="1980643",
        source_url="https://www.pixiv.net/users/1980643",
        is_enabled=True,
        auth_healthy=True,
        auth_status="ok",
        auth_error_reason=None,
        last_auth_checked_at=_dt(),
        last_successful_auth=_dt(),
        last_synced_at=_dt(),
        last_attempted_at=_dt(),
        created_at=_dt(),
        updated_at=_dt(),
    )
    sub = SimpleNamespace(
        id=sub_id,
        name="Pixiv daily",
        is_active=True,
        sync_enabled=True,
        sync_interval_hours=6,
        schedule_mode="interval",
        scheduled_times=None,
        last_synced_at=_dt(),
    )
    creator = SimpleNamespace(
        id=creator_id,
        name="askzy",
        display_name="ASK",
        thumbnail_url=None,
        is_favorite=True,
    )
    return ss, sub, creator


def test_repository_detail_returns_context_jobs_and_recent_works():
    from app.api.repositories import get_repository

    source_id = uuid4()
    sub_id = uuid4()
    creator_id = uuid4()
    job_id = uuid4()
    work_id = uuid4()
    ss, sub, creator = _source_context(source_id, sub_id, creator_id)
    job = SimpleNamespace(
        id=job_id,
        subscription_id=sub_id,
        subscription_source_id=source_id,
        source="pixiv",
        source_url=ss.source_url,
        status="complete",
        retry_count=0,
        error_log=None,
        created_at=_dt(),
        updated_at=_dt(),
    )
    work = SimpleNamespace(
        id=work_id,
        title="Test Work",
        posted_at="2026-06-01",
        thumbnail_asset_id="asset-1",
        is_nsfw=False,
        is_ai_generated=False,
        is_favorite=False,
        created_at=_dt(),
    )
    work_source = SimpleNamespace(source="pixiv", title="Source Title", posted_at="2026-06-01")
    db = _FakeDB([
        _Result(first=(ss, sub, creator)),
        _Result(scalars=[job]),
        _Result(rows=[(work, work_source, 2)]),
    ])

    payload = asyncio.run(get_repository(source_id, db=db))

    assert payload["repository"]["id"] == str(source_id)
    assert payload["repository"]["latest_job"]["id"] == str(job_id)
    assert payload["creator"]["display_name"] == "ASK"
    assert payload["subscription"]["name"] == "Pixiv daily"
    assert payload["provider"]["url_valid"] is True
    assert payload["recent_jobs"][0]["subscription_source_id"] == str(source_id)
    assert payload["recent_works"][0]["id"] == str(work_id)
    assert payload["recent_works"][0]["asset_count"] == 2


def test_repository_detail_returns_404_for_missing_source():
    from app.api.repositories import get_repository

    db = _FakeDB([_Result(first=None)])

    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_repository(uuid4(), db=db))

    assert exc.value.status_code == 404


def test_repository_sync_now_delegates_to_existing_enqueue(monkeypatch):
    from app.api import repositories

    source_id = uuid4()
    sub_id = uuid4()
    creator_id = uuid4()
    ss, sub, creator = _source_context(source_id, sub_id, creator_id)
    db = _FakeDB([_Result(first=(ss, sub, creator))])

    async def fake_enqueue(received_db, received_source_id, trigger):
        assert received_db is db
        assert received_source_id == source_id
        assert trigger == "manual_repository"
        return {"status": "skipped", "reason": "source_disabled"}

    monkeypatch.setattr(repositories, "enqueue_subscription_source_sync", fake_enqueue)

    payload = asyncio.run(repositories.sync_repository(source_id, db=db))

    assert payload == {"status": "skipped", "reason": "source_disabled"}
