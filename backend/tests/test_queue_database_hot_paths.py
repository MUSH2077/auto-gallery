"""Regression contracts for the queue and database hot-path maintenance."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, or_, select, text
from sqlalchemy.dialects import postgresql


def _postgres_sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


def test_shadow_readiness_is_indexed_exists_sql_without_gitllery(monkeypatch):
    from app.services import outbox_coordinator

    monkeypatch.setattr(
        outbox_coordinator.settings,
        "gitllery_projection_mode",
        "shadow",
    )

    sql = _postgres_sql(outbox_coordinator.outbox_readiness_statement())

    assert "exists (" in sql
    assert "count(" not in sql
    assert "gitllery_projection" not in sql
    assert "curation_commits" not in sql
    assert "gitllery_repository" not in sql


def test_active_readiness_adds_gitllery_only_when_enabled(monkeypatch):
    from app.services import outbox_coordinator

    monkeypatch.setattr(
        outbox_coordinator.settings,
        "gitllery_projection_mode",
        "active",
    )

    sql = _postgres_sql(outbox_coordinator.outbox_readiness_statement())

    assert "gitllery_projection_outbox" in sql
    assert "gitllery_projection_targets" in sql


@pytest.mark.asyncio
async def test_exact_outbox_health_is_cached_for_thirty_seconds(monkeypatch):
    from app.services import outbox_coordinator

    snapshot = {
        "search": {
            "waiting": 1,
            "processing": 0,
            "failed": 0,
            "oldest_age_seconds": 2.0,
        }
    }
    loader = AsyncMock(return_value=snapshot)
    clock = iter((10.0, 20.0, 41.0))
    monkeypatch.setattr(outbox_coordinator, "_load_outbox_health", loader)
    monkeypatch.setattr(outbox_coordinator, "monotonic", lambda: next(clock))
    outbox_coordinator.invalidate_outbox_health_cache()

    try:
        assert await outbox_coordinator.outbox_health(SimpleNamespace()) == snapshot
        assert await outbox_coordinator.outbox_health(SimpleNamespace()) == snapshot
        assert await outbox_coordinator.outbox_health(SimpleNamespace()) == snapshot

        assert outbox_coordinator.OUTBOX_HEALTH_CACHE_TTL_SECONDS == 30.0
        assert loader.await_count == 2
    finally:
        outbox_coordinator.invalidate_outbox_health_cache()


def test_http_lifespan_has_no_outbox_coordinator_and_health_reads_shared_cache():
    from app import main

    lifespan_source = inspect.getsource(main.lifespan)
    health_source = inspect.getsource(main._build_health_snapshot)

    assert "outbox_coordinator_loop" not in lifespan_source
    assert "outbox_coordinator_task" not in lifespan_source
    assert "read_published_outbox_health" in health_source
    assert "outbox_health(session)" not in health_source


def test_scheduler_supervisor_owns_sixty_second_outbox_fallback():
    import worker_entrypoint

    source = inspect.getsource(worker_entrypoint.main)

    assert worker_entrypoint.OUTBOX_FALLBACK_INTERVAL_SECONDS == 60
    assert '"scheduled" in supervised_queues' in source
    assert "run_outbox_control_plane_tick" in source


def test_write_wake_is_armed_only_after_outer_commit(monkeypatch):
    from app.services import outbox_coordinator

    wake = []
    monkeypatch.setattr(
        outbox_coordinator,
        "wake_pending_outboxes",
        lambda counts, **_kwargs: wake.append(counts)
        or {"enqueued": 1, "deferred": 0},
    )
    transaction = object()
    session = SimpleNamespace(
        info={},
        in_nested_transaction=lambda: False,
        get_transaction=lambda: transaction,
        get_nested_transaction=lambda: None,
    )
    db = SimpleNamespace(info=session.info, sync_session=session)

    outbox_coordinator.mark_outbox_wake_pending(db, "media")
    assert wake == []
    outbox_coordinator._arm_outbox_wakes_after_outer_commit(session)
    assert wake == []
    outbox_coordinator._publish_outbox_wakes_after_commit(session)

    assert wake == [{"media": 1}]


def test_rolled_back_write_does_not_publish_a_wake(monkeypatch):
    from app.services import outbox_coordinator

    wake = []
    monkeypatch.setattr(
        outbox_coordinator,
        "wake_pending_outboxes",
        lambda counts, **_kwargs: wake.append(counts),
    )
    class Transaction:
        parent = None

    transaction = Transaction()
    session = SimpleNamespace(
        info={},
        in_nested_transaction=lambda: False,
        get_transaction=lambda: transaction,
        get_nested_transaction=lambda: None,
    )
    db = SimpleNamespace(info=session.info, sync_session=session)

    outbox_coordinator.mark_outbox_wake_pending(db, "search")
    outbox_coordinator._discard_rolled_back_outbox_wakes(session, transaction)
    outbox_coordinator._arm_outbox_wakes_after_outer_commit(session)
    outbox_coordinator._publish_outbox_wakes_after_commit(session)

    assert wake == []


def test_phash_band_offsets_compile_as_expression_index_literals():
    from app.models import Asset
    from app.services.asset_reconciliation import phash_candidate_conditions

    anchor = SimpleNamespace(
        sha256=None,
        phash="0123456789abcdef",
        phash_version="imagehash-phash-v1",
    )
    sql = str(
        Asset.__table__.select()
        .where(*phash_candidate_conditions(anchor))
        .compile(dialect=postgresql.dialect())
    ).lower()

    for start, length in ((1, 3), (4, 3), (7, 3), (10, 3), (13, 4)):
        assert f"substr(assets.phash, {start}, {length})" in sql


def test_storage_artifact_import_index_migration_is_concurrent_and_extends_head():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "fe46f80abc24_add_storage_artifact_import_index.py"
    )
    source = migration_path.read_text()

    assert 'revision: str = "fe46f80abc24"' in source
    assert 'down_revision: str = "fd35e7f9ab13"' in source
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in source
    assert "ix_storage_artifacts_import_job_id" in source
    assert "storage_artifacts (import_job_id)" in source
    assert "autocommit_block" in source


def test_completed_cleanup_excludes_gitllery_and_caps_each_transaction():
    from app.services import outbox_coordinator

    specs = outbox_coordinator.completed_outbox_cleanup_specs(
        datetime.now(timezone.utc) - timedelta(days=30),
        batch_size=500,
    )

    assert specs
    assert all(spec.limit == 500 for spec in specs)
    assert all("gitllery" not in spec.model.__tablename__ for spec in specs)
    assert {spec.model.__tablename__ for spec in specs} == {
        "search_projection_outbox",
        "media_derivative_outbox",
        "asset_dedup_outbox",
        "import_curation_outbox",
    }


@pytest.mark.asyncio
async def test_outbox_request_marks_matching_post_commit_wake(monkeypatch):
    from app.services import media_derivatives

    marked = []
    monkeypatch.setattr(
        media_derivatives,
        "mark_outbox_wake_pending",
        lambda db, kind: marked.append((db, kind)),
    )

    class Result:
        pass

    class Session:
        async def execute(self, _statement):
            return Result()

    db = Session()
    count = await media_derivatives.request_media_derivatives(
        db,
        [
            {
                "asset_id": "a6743b42-3c34-4aba-b8b0-a1b43b09f0ea",
                "requested": {"thumbnail": True},
                "source_size": 1,
                "source_mtime_ns": 2,
            }
        ],
    )

    assert count == 1
    assert marked == [(db, "media")]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_outer_commit_wakes_once_and_rollback_never_wakes(monkeypatch):
    from app.database import async_session, engine
    from app.models import SearchProjectionOutbox
    from app.services import outbox_coordinator

    wake = []
    monkeypatch.setattr(
        outbox_coordinator,
        "wake_pending_outboxes",
        lambda counts, **_kwargs: wake.append(counts)
        or {"enqueued": 1, "deferred": 0},
    )
    try:
        async with async_session() as db:
            await db.execute(text("DELETE FROM search_projection_outbox"))
            await db.commit()
            db.add(
                SearchProjectionOutbox(
                    index_uid="wake-test",
                    entity_id=str(uuid4()),
                    action="upsert",
                    available_at=datetime.now(timezone.utc),
                )
            )
            outbox_coordinator.mark_outbox_wake_pending(db, "search")
            assert wake == []
            await db.commit()
            assert wake == [{"search": 1}]

            db.add(
                SearchProjectionOutbox(
                    index_uid="wake-test",
                    entity_id=str(uuid4()),
                    action="upsert",
                    available_at=datetime.now(timezone.utc),
                )
            )
            outbox_coordinator.mark_outbox_wake_pending(db, "search")
            await db.rollback()
            assert wake == [{"search": 1}]
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM search_projection_outbox"))
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_completed_cleanup_deletes_only_one_batch_and_keeps_pending():
    from app.database import async_session, engine
    from app.models import SearchProjectionOutbox
    from app.services.outbox_coordinator import cleanup_completed_outboxes

    old = datetime.now(timezone.utc) - timedelta(days=31)
    rows = [
        {
            "index_uid": "cleanup-test",
            "entity_id": str(uuid4()),
            "action": "upsert",
            "available_at": old,
            "completed_at": old,
        }
        for _ in range(501)
    ]
    rows.append(
        {
            "index_uid": "cleanup-test",
            "entity_id": str(uuid4()),
            "action": "upsert",
            "available_at": old,
            "completed_at": None,
        }
    )
    try:
        async with async_session() as db:
            await db.execute(
                text(
                    "DELETE FROM search_projection_outbox "
                    "WHERE index_uid = 'cleanup-test'"
                )
            )
            await db.execute(insert(SearchProjectionOutbox), rows)
            await db.commit()

            result = await cleanup_completed_outboxes(db)

            assert result["deleted"] == 500
            remaining = (
                await db.execute(
                    select(
                        func.count(SearchProjectionOutbox.id),
                        func.count(SearchProjectionOutbox.id).filter(
                            SearchProjectionOutbox.completed_at.is_(None)
                        ),
                    ).where(SearchProjectionOutbox.index_uid == "cleanup-test")
                )
            ).one()
            assert tuple(remaining) == (2, 1)
    finally:
        async with async_session() as db:
            await db.execute(
                text(
                    "DELETE FROM search_projection_outbox "
                    "WHERE index_uid = 'cleanup-test'"
                )
            )
            await db.commit()
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_explain_uses_phash_expression_and_import_job_indexes():
    from app.database import async_session, engine
    from app.models import Asset
    from app.services.asset_reconciliation import phash_candidate_conditions

    try:
        async with async_session() as db:
            for index, (start, length) in enumerate(
                ((1, 3), (4, 3), (7, 3), (10, 3), (13, 4))
            ):
                await db.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS ix_assets_phash_band_{index} "
                        f"ON assets ((substr(phash, {start}, {length}))) "
                        "WHERE phash IS NOT NULL"
                    )
                )
            await db.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS "
                    "ix_storage_artifacts_import_job_id "
                    "ON storage_artifacts (import_job_id)"
                )
            )
            await db.commit()
            await db.execute(text("SET LOCAL enable_seqscan = off"))

            anchor = SimpleNamespace(
                phash="0123456789abcdef",
                phash_version="imagehash-phash-v1",
            )
            candidate_sql = _postgres_sql(
                select(Asset.id).where(
                    or_(*phash_candidate_conditions(anchor))
                )
            )
            phash_plan = (
                await db.execute(text(f"EXPLAIN (FORMAT JSON) {candidate_sql}"))
            ).scalar_one()
            import_plan = (
                await db.execute(
                    text(
                        "EXPLAIN (FORMAT JSON) "
                        "SELECT id FROM storage_artifacts "
                        "WHERE import_job_id = "
                        "'00000000-0000-0000-0000-000000000001'::uuid"
                    )
                )
            ).scalar_one()

            assert "ix_assets_phash_band_" in str(phash_plan)
            assert "ix_storage_artifacts_import_job_id" in str(import_plan)
            await db.rollback()
    finally:
        await engine.dispose()
