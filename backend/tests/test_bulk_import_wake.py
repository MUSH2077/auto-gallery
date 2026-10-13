"""Transactional wake-up behavior of the real bulk importer SQL path."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
async def bulk_environment(tmp_path, monkeypatch):
    from app.config import settings
    from app.database import async_session, engine
    from app.services.redis_client import get_redis

    wake_keys = tuple(
        f"outbox:wakeup:{kind}"
        for kind in ("media", "import_projection", "search")
    )
    redis_client = get_redis()
    redis_client.delete(*wake_keys)

    monkeypatch.setattr(settings, "library_root", str(tmp_path / "library"))
    async with async_session() as db:
        await db.execute(text("TRUNCATE works, assets, search_projection_outbox CASCADE"))
        await db.commit()
        try:
            yield db, Path(settings.library_root)
        finally:
            await db.rollback()
            await db.execute(text("TRUNCATE works, assets, search_projection_outbox CASCADE"))
            await db.commit()
            redis_client.delete(*wake_keys)
    await engine.dispose()


async def _staged_work(db, library):
    from app.models import Asset, Work

    work = Work(title="Bulk wake regression")
    asset = Asset(file_path=f"bulk-wake/{uuid4()}.jpg", file_name="page.jpg")
    db.add_all([work, asset])
    await db.flush()
    staged = {
        "work": work,
        "work_id": work.id,
        "source_work_id": str(work.id),
        "lib_dir": library / "manual" / "bulk-wake" / str(work.id),
        "derivative_requests": [{"asset_id": asset.id, "requested": {"thumbnail": True}}],
        "import_projection": {"source": "manual", "creator_id": None, "repository_id": None},
        "prepared_work": {"tags": []},
    }
    return staged


async def _persist(db, staged):
    from app.jobs.import_runner import _execute_bulk_outboxes

    job = SimpleNamespace(subscription_id=None, subscription_source_id=None)
    return await _execute_bulk_outboxes(db, staged, job)


async def _counts(db, staged):
    from app.models import ImportCurationOutbox, MediaDerivativeOutbox, SearchProjectionOutbox

    work_id = staged["work_id"]
    asset_id = staged["derivative_requests"][0]["asset_id"]
    return (
        await db.scalar(select(func.count()).select_from(MediaDerivativeOutbox).where(MediaDerivativeOutbox.asset_id == asset_id)),
        await db.scalar(select(func.count()).select_from(ImportCurationOutbox).where(ImportCurationOutbox.work_id == work_id)),
        await db.scalar(select(func.count()).select_from(SearchProjectionOutbox).where(SearchProjectionOutbox.entity_id == str(work_id))),
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["commit", "rollback", "savepoint_commit_then_rollback", "savepoint_rollback"])
async def test_bulk_wake_waits_for_outer_commit(bulk_environment, monkeypatch, outcome):
    from app.services import outbox_coordinator

    db, library = bulk_environment
    staged = await _staged_work(db, library)
    await db.commit()
    published = []
    monkeypatch.setattr(outbox_coordinator, "wake_pending_outboxes", lambda counts, **_: published.append(counts))
    if outcome.startswith("savepoint"):
        nested = await db.begin_nested()
        await _persist(db, [staged])
        assert published == []
        if outcome == "savepoint_rollback":
            await nested.rollback()
            await db.commit()
        else:
            await nested.commit()
            assert published == []
            await db.rollback()
    else:
        await _persist(db, [staged])
        assert published == []
        if outcome == "commit":
            await db.commit()
        else:
            await db.rollback()
    assert published == ([{"import_projection": 1, "media": 1, "search": 1}] if outcome == "commit" else [])
    assert await _counts(db, staged) == ((1, 1, 1) if outcome == "commit" else (0, 0, 0))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_repeated_requests_coalesce_within_one_commit(bulk_environment, monkeypatch):
    from app.services import outbox_coordinator

    db, library = bulk_environment
    staged = await _staged_work(db, library)
    await db.commit()
    published = []
    monkeypatch.setattr(outbox_coordinator, "wake_pending_outboxes", lambda counts, **_: published.append(counts))
    await _persist(db, [staged])
    await _persist(db, [staged])
    assert published == []
    await db.commit()
    assert published == [{"import_projection": 1, "media": 1, "search": 1}]
    assert await _counts(db, staged) == (1, 1, 1)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failed_bulk_savepoint_preserves_successful_batch_wake(bulk_environment, monkeypatch):
    from app.services import outbox_coordinator

    db, library = bulk_environment
    good = await _staged_work(db, library)
    bad = await _staged_work(db, library)
    await db.commit()
    published = []
    monkeypatch.setattr(outbox_coordinator, "wake_pending_outboxes", lambda counts, **_: published.append(counts))
    async with db.begin_nested():
        await _persist(db, [good])
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            bad["derivative_requests"][0]["asset_id"] = uuid4()
            await _persist(db, [bad])
    assert published == []
    await db.commit()
    assert published == [{"import_projection": 1, "media": 1, "search": 1}]
    assert await _counts(db, good) == (1, 1, 1)
    assert await _counts(db, bad) == (0, 0, 0)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["redis", "enqueue"])
async def test_bulk_commit_survives_notification_failure_and_can_retry(bulk_environment, monkeypatch, failure):
    from app.services import outbox_coordinator
    from app.services.redis_client import get_redis

    db, library = bulk_environment
    staged = await _staged_work(db, library)
    await db.commit()
    real_client = get_redis()
    queued = []
    original_redis = outbox_coordinator.get_redis
    with monkeypatch.context() as failing:
        def unavailable(*_args, **_kwargs):
            raise RedisConnectionError("injected bulk wake failure")
        failing.setattr(outbox_coordinator, "get_redis" if failure == "redis" else "checked_enqueue", unavailable)
        await _persist(db, [staged])
        await db.commit()
    assert await _counts(db, staged) == (1, 1, 1)
    counts = await outbox_coordinator.outbox_counts(db, ready_only=True)
    wanted = {kind: counts[kind] for kind in ("media", "import_projection", "search")}
    assert wanted == {"media": 1, "import_projection": 1, "search": 1}
    for kind in wanted:
        assert not real_client.exists(f"outbox:wakeup:{kind}")
    monkeypatch.setattr(outbox_coordinator, "get_redis", original_redis)
    monkeypatch.setattr(outbox_coordinator, "checked_enqueue", lambda queue, function, *args, **kwargs: queued.append((function, kwargs["job_id"])))
    try:
        assert outbox_coordinator.wake_pending_outboxes(wanted) == {"enqueued": 3, "deferred": 0}
        assert len(queued) == 3
    finally:
        for kind in wanted:
            real_client.delete(f"outbox:wakeup:{kind}")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_bulk_does_not_notify(bulk_environment, monkeypatch):
    from app.services import outbox_coordinator

    db, _library = bulk_environment
    monkeypatch.setattr(outbox_coordinator, "wake_pending_outboxes", lambda *_args, **_kwargs: pytest.fail("empty bulk notified workers"))
    assert await _persist(db, []) == (0, 0)
    await db.commit()
