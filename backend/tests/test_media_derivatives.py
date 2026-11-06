import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.services import media_derivatives


def _permit_media_candidate(monkeypatch, asset_id):
    async def candidate():
        return asset_id, "image_derive"

    @asynccontextmanager
    async def permit(*_args, **_kwargs):
        yield SimpleNamespace(work_units=1, slice_seconds=20.0)

    monkeypatch.setattr(media_derivatives, "_next_resource_candidate", candidate)
    monkeypatch.setattr(
        "app.services.heavy_io.adaptive_resource_slice",
        permit,
    )


class _ExecuteResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _RecordingSession:
    def __init__(self, results=()):
        self.statements = []
        self.results = list(results)
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if self.results:
            return self.results.pop(0)
        return _ExecuteResult()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return None


def _sql(statement):
    return statement.compile(dialect=postgresql.dialect())


def test_merge_requests_is_boolean_or():
    assert media_derivatives._merge_requests(
        {"video": True, "thumbnail": False},
        {"video": False, "thumbnail": True, "ignored": False},
    ) == {"video": True, "thumbnail": True}


def test_request_batch_coalesces_duplicates_and_uses_atomic_upsert():
    asset_id = uuid4()
    db = _RecordingSession()

    count = asyncio.run(
        media_derivatives.request_media_derivatives(
            db,
            [
                {
                    "asset_id": asset_id,
                    "requested": {"video": True, "thumbnail": False},
                    "source_size": 10,
                    "source_mtime_ns": 100,
                },
                {
                    "asset_id": asset_id,
                    "requested": {"video": False, "thumbnail": True},
                    "source_size": 20,
                    "source_mtime_ns": 200,
                },
            ],
        )
    )

    assert count == 1
    assert len(db.statements) == 1
    compiled = _sql(db.statements[0])
    sql = str(compiled)
    assert "ON CONFLICT (asset_id) DO UPDATE" in sql
    assert "media_derivative_outbox.requested || excluded.requested" in sql
    assert "state =" in sql
    assert "lease_expires_at =" in sql
    assert {"video": True, "thumbnail": True} in compiled.params.values()
    assert 20 in compiled.params.values()
    assert 200 in compiled.params.values()


@pytest.mark.parametrize("operation", ["complete", "fail"])
def test_stale_claim_cannot_complete_or_fail_newer_intent(monkeypatch, operation):
    db = _RecordingSession([_ExecuteResult(None)])
    monkeypatch.setattr(
        media_derivatives,
        "async_session",
        lambda: _SessionContext(db),
    )
    token = datetime(2030, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
    request = {
        "id": uuid4(),
        "asset_id": uuid4(),
        "lease_expires_at": token,
        "attempts": 1,
    }

    if operation == "complete":
        applied = asyncio.run(
            media_derivatives._complete(request, {"sha256": "a" * 64})
        )
    else:
        applied = asyncio.run(
            media_derivatives._fail(request, RuntimeError("failed"))
        )

    assert applied is False
    assert db.commits == 0
    assert db.rollbacks == 1
    # A stale completion must not reach the Asset UPDATE.
    assert len(db.statements) == 1
    compiled = _sql(db.statements[0])
    assert "state" in str(compiled)
    assert "lease_expires_at" in str(compiled)
    assert token in compiled.params.values()


def test_completion_persists_dedup_observation_in_same_transaction(monkeypatch):
    claim_id = uuid4()
    asset_id = uuid4()
    db = _RecordingSession([_ExecuteResult(claim_id)])
    monkeypatch.setattr(
        media_derivatives,
        "async_session",
        lambda: _SessionContext(db),
    )
    request = {
        "id": claim_id,
        "asset_id": asset_id,
        "lease_expires_at": datetime.now(timezone.utc),
        "attempts": 1,
        "requested": {"sha256": True, "dedup": True},
        "algorithm_version": media_derivatives.MEDIA_ALGORITHM_VERSION,
        "source_size": 10,
        "source_mtime_ns": 20,
    }

    applied = asyncio.run(
        media_derivatives._complete(
            request,
            {
                "sha256": "a" * 64,
                "derivative_source_size": 10,
                "derivative_source_mtime_ns": 20,
            },
        )
    )

    assert applied is True
    assert db.commits == 1
    assert len(db.statements) == 3
    observe = _sql(db.statements[2])
    assert "INSERT INTO asset_dedup_outbox" in str(observe)
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in str(observe)
    assert "observe" in observe.params.values()
    assert str(asset_id) in str(observe.params.values())


def _image_descriptor(source: Path, library_dir: Path, stat) -> dict:
    return {
        "file_path": source.name,
        "file_name": source.name,
        "mime_type": "image/jpeg",
        "width": 40,
        "height": 30,
        "duration": None,
        "sha256": None,
        "phash": None,
        "thumb_sm_path": "missing.webp",
        "thumb_lg_path": None,
        "derivative_version": media_derivatives.MEDIA_ALGORITHM_VERSION,
        "derivative_source_size": stat.st_size,
        "derivative_source_mtime_ns": stat.st_mtime_ns,
        "library_dir": str(library_dir),
    }


def _thumbnail_request(stat) -> dict:
    return {
        "requested": {"thumbnail": True, "dimensions": True},
        "algorithm_version": media_derivatives.MEDIA_ALGORITHM_VERSION,
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
    }


def test_missing_file_behind_existing_db_path_is_rebuilt(tmp_path, monkeypatch):
    download_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    download_root.mkdir()
    library_root.mkdir()
    source = download_root / "asset.jpg"
    source.write_bytes(b"source")
    stat = source.stat()
    library_dir = library_root / "creator" / "work"
    calls = []

    monkeypatch.setattr(media_derivatives.settings, "download_root", str(download_root))
    monkeypatch.setattr(media_derivatives.settings, "library_root", str(library_root))
    monkeypatch.setattr(media_derivatives, "can_generate_thumbnail", lambda _suffix: True)

    def generate(_source, target_dir, name):
        calls.append((_source, target_dir, name))
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{name}.webp"
        target.write_bytes(b"webp")
        return str(target), 40, 30

    monkeypatch.setattr(media_derivatives, "inspect_and_generate_thumbnail", generate)
    values = media_derivatives._derive_sync(
        _image_descriptor(source, library_dir, stat),
        _thumbnail_request(stat),
    )

    assert len(calls) == 1
    assert values["thumb_sm_path"] == "creator/work/asset.thumbnail.webp"
    assert (library_root / values["thumb_sm_path"]).stat().st_size > 0


def test_missing_or_empty_requested_output_fails_for_retry(tmp_path, monkeypatch):
    download_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    download_root.mkdir()
    library_root.mkdir()
    source = download_root / "asset.jpg"
    source.write_bytes(b"source")
    stat = source.stat()

    monkeypatch.setattr(media_derivatives.settings, "download_root", str(download_root))
    monkeypatch.setattr(media_derivatives.settings, "library_root", str(library_root))
    monkeypatch.setattr(media_derivatives, "can_generate_thumbnail", lambda _suffix: True)
    monkeypatch.setattr(
        media_derivatives,
        "inspect_and_generate_thumbnail",
        lambda *_args, **_kwargs: (None, None, None),
    )

    with pytest.raises(RuntimeError, match="thumbnail was not produced"):
        media_derivatives._derive_sync(
            _image_descriptor(source, library_root / "creator" / "work", stat),
            _thumbnail_request(stat),
        )


def test_empty_video_outputs_force_rebuild_even_when_fingerprint_matches(
    tmp_path, monkeypatch
):
    download_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    library_dir = library_root / "creator" / "work"
    download_root.mkdir()
    library_dir.mkdir(parents=True)
    source = download_root / "asset.mp4"
    source.write_bytes(b"source")
    stat = source.stat()
    thumbnail = library_dir / "asset.thumbnail.webp"
    poster = library_dir / "asset.poster.webp"
    thumbnail.touch()
    poster.touch()
    force_values = []

    monkeypatch.setattr(media_derivatives.settings, "download_root", str(download_root))
    monkeypatch.setattr(media_derivatives.settings, "library_root", str(library_root))
    monkeypatch.setattr(media_derivatives, "can_generate_thumbnail", lambda _suffix: False)

    def render(_source, _target, _name, *, force):
        force_values.append(force)
        thumbnail.write_bytes(b"small")
        poster.write_bytes(b"large")
        return SimpleNamespace(
            inspection=SimpleNamespace(width=1920, height=1080, duration=3.0),
            thumbnail_path=thumbnail,
            poster_path=poster,
            error=None,
        )

    monkeypatch.setattr(media_derivatives, "render_video_derivatives", render)
    descriptor = _image_descriptor(source, library_dir, stat)
    descriptor.update(
        file_name="asset.mp4",
        mime_type="video/mp4",
        thumb_sm_path="creator/work/asset.thumbnail.webp",
        thumb_lg_path="creator/work/asset.poster.webp",
    )
    request = _thumbnail_request(stat)
    request["requested"]["video"] = True

    values = media_derivatives._derive_sync(descriptor, request)

    assert force_values == [True]
    assert values["thumb_sm_path"] == "creator/work/asset.thumbnail.webp"
    assert values["thumb_lg_path"] == "creator/work/asset.poster.webp"


def test_descriptor_load_failure_releases_claim_as_failed(monkeypatch):
    request = {
        "id": uuid4(),
        "asset_id": uuid4(),
        "requested": {},
        "lease_expires_at": datetime.now(timezone.utc),
        "attempts": 1,
    }
    claims = iter((request, None))
    failures = []

    async def claim_one(_asset_id=None):
        return next(claims)

    async def load(_asset_id):
        raise RuntimeError("descriptor unavailable")

    async def fail(claim, exc):
        failures.append((claim, str(exc)))
        return True

    monkeypatch.setattr(media_derivatives, "_claim_one", claim_one)
    _permit_media_candidate(monkeypatch, request["asset_id"])
    monkeypatch.setattr(media_derivatives, "_load_asset_descriptor", load)
    monkeypatch.setattr(media_derivatives, "_fail", fail)

    result = asyncio.run(
        media_derivatives.process_media_derivative_outbox(limit=1)
    )

    assert result == {
        "claimed": 1,
        "processed": 0,
        "failed": 1,
        "slice_exhausted": True,
        "more_likely": True,
    }
    assert failures == [(request, "descriptor unavailable")]


def test_stale_missing_asset_completion_is_not_counted(monkeypatch):
    request = {
        "id": uuid4(),
        "asset_id": uuid4(),
        "requested": {},
        "lease_expires_at": datetime.now(timezone.utc),
        "attempts": 1,
    }
    claims = iter((request, None))

    async def claim_one(_asset_id=None):
        return next(claims)

    async def missing(_asset_id):
        return None

    async def stale_complete(_request, _values):
        return False

    monkeypatch.setattr(media_derivatives, "_claim_one", claim_one)
    _permit_media_candidate(monkeypatch, request["asset_id"])
    monkeypatch.setattr(media_derivatives, "_load_asset_descriptor", missing)
    monkeypatch.setattr(media_derivatives, "_complete", stale_complete)

    result = asyncio.run(
        media_derivatives.process_media_derivative_outbox(limit=1)
    )

    assert result == {
        "claimed": 1,
        "processed": 0,
        "failed": 0,
        "slice_exhausted": True,
        "more_likely": True,
    }


@pytest.fixture
async def candidate_store(monkeypatch, tmp_path):
    """Real PostgreSQL rows; only the resource snapshot and clock are fixed."""
    import os
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from app.config import settings
    from app.models import Asset, MediaDerivativeOutbox
    from app.services import resource_pressure
    from tests.test_parallel_media_rq import _normal_snapshot

    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    clock = SimpleNamespace(now=datetime.now(timezone.utc))
    ids = []
    snapshot = _normal_snapshot()
    async def pressure():
        return snapshot
    monkeypatch.setattr(media_derivatives, "async_session", sessions)
    monkeypatch.setattr(media_derivatives, "_now", lambda: clock.now)
    monkeypatch.setattr(resource_pressure, "get_resource_pressure_snapshot", pressure)
    monkeypatch.setattr(settings, "media_worker_concurrency", 4)
    monkeypatch.setenv("HEAVY_IO_LOCK_PATH", str(tmp_path / "heavy-io.lock"))

    async def add(*, at=None, thumbnail=False, existing=False, video=False,
                  state="pending", lease=None):
        asset = Asset(file_path=f"priority/{uuid4()}.{'mp4' if video else 'png'}",
                      file_name="media.mp4" if video else "media.png",
                      mime_type="video/mp4" if video else "image/png",
                      thumb_sm_path="ready.webp" if existing else None)
        async with sessions() as db:
            db.add(asset)
            await db.flush()
            intent = MediaDerivativeOutbox(asset_id=asset.id,
                requested={"sha256": True, **({"thumbnail": True} if thumbnail else {})},
                available_at=at if at is not None else clock.now,
                state=state, lease_expires_at=lease)
            db.add(intent)
            await db.commit()
        ids.append(asset.id)
        return asset.id

    try:
        yield SimpleNamespace(add=add, sessions=sessions, clock=clock, snapshot=snapshot)
    finally:
        async with sessions() as db:
            await db.execute(delete(Asset).where(Asset.id.in_(ids)))
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_missing_thumbnail_precedes_recent_fingerprint(candidate_store):
    store = candidate_store
    fingerprint = await store.add(at=store.clock.now - timedelta(seconds=10))
    missing = await store.add(at=store.clock.now - timedelta(seconds=1), thumbnail=True)
    assert await media_derivatives._next_resource_candidate() == (missing, "image_derive")
    request = await media_derivatives._claim_one(missing)
    assert request["asset_id"] == missing
    assert await media_derivatives._next_resource_candidate() == (fingerprint, "image_derive")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_old_fingerprint_runs_despite_continuing_missing_thumbnails(candidate_store):
    from sqlalchemy import update
    from app.models import MediaDerivativeOutbox
    store = candidate_store
    origin = store.clock.now
    fingerprint = await store.add(at=origin)
    served_thumbnails = 0
    for second in range(1, 24):
        store.clock.now = origin + timedelta(seconds=second)
        await store.add(thumbnail=True)
        selected, workload = await media_derivatives._next_resource_candidate()
        assert workload == "image_derive"
        if selected == fingerprint:
            break
        served_thumbnails += 1
        async with store.sessions() as db:
            await db.execute(update(MediaDerivativeOutbox).where(
                MediaDerivativeOutbox.asset_id == selected).values(state="complete"))
            await db.commit()
    assert served_thumbnails > 0
    assert selected == fingerprint


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("state,lease_offset,ready", [
    ("pending", None, False), ("failed", None, False),
    ("processing", 30, False), ("processing", -1, True),
])
async def test_pg_thumbnail_priority_preserves_due_time_and_lease(candidate_store, state, lease_offset, ready):
    store = candidate_store
    fingerprint = await store.add(at=store.clock.now - timedelta(seconds=1))
    available = store.clock.now + timedelta(seconds=5) if state != "processing" else store.clock.now - timedelta(seconds=5)
    missing = await store.add(at=available, thumbnail=True, state=state,
                             lease=store.clock.now + timedelta(seconds=lease_offset) if lease_offset is not None else None)
    assert await media_derivatives._next_resource_candidate() == ((missing if ready else fingerprint), "image_derive")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_existing_thumbnail_does_not_receive_priority(candidate_store):
    store = candidate_store
    fingerprint = await store.add(at=store.clock.now - timedelta(seconds=5))
    await store.add(thumbnail=True, existing=True)
    assert await media_derivatives._next_resource_candidate() == (fingerprint, "image_derive")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("missing_video", [False, True])
async def test_pg_thumbnail_rank_is_used_across_admissible_profiles(candidate_store, missing_video):
    store = candidate_store
    await store.add(at=store.clock.now - timedelta(seconds=5), video=not missing_video)
    missing = await store.add(thumbnail=True, video=missing_video)
    assert await media_derivatives._next_resource_candidate() == (
        missing, "video_derive" if missing_video else "image_derive")
