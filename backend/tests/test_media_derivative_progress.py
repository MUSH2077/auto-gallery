from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text, update


async def _clear_tables(db):
    await db.execute(text("""
        TRUNCATE
            media_derivative_outbox,
            asset_sources,
            assets,
            work_sources,
            works
        RESTART IDENTITY CASCADE
    """))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_media_derivative_progress_reports_thumbnail_completion_and_stall():
    from app.database import async_session, engine
    from app.models import Asset, AssetSource, MediaDerivativeOutbox, Work, WorkSource
    from app.services import media_derivatives

    reader = getattr(media_derivatives, "media_derivative_progress", None)
    assert callable(reader), "media derivative progress reader is not implemented"

    now = datetime.now(timezone.utc)
    try:
        async with async_session() as db:
            await _clear_tables(db)

            works = [
                Work(title="Progress one", is_nsfw=False, is_ai_generated=False),
                Work(title="Progress two", is_nsfw=False, is_ai_generated=False),
            ]
            db.add_all(works)
            await db.flush()
            sources = [
                WorkSource(work_id=works[0].id, source="pixiv", source_work_id="progress-1"),
                WorkSource(work_id=works[1].id, source="pixiv", source_work_id="progress-2"),
            ]
            db.add_all(sources)
            await db.flush()

            assets = [
                Asset(file_path="pixiv/progress/ready.jpg", file_name="ready.jpg", mime_type="image/jpeg", thumb_sm_path="pixiv/progress/ready.thumbnail.webp"),
                Asset(file_path="pixiv/progress/pending.jpg", file_name="pending.jpg", mime_type="image/jpeg"),
                Asset(file_path="pixiv/progress/processing.jpg", file_name="processing.jpg", mime_type="image/jpeg"),
                Asset(file_path="pixiv/progress/failed.jpg", file_name="failed.jpg", mime_type="image/jpeg"),
            ]
            db.add_all(assets)
            await db.flush()
            db.add_all([
                AssetSource(asset_id=assets[0].id, work_source_id=sources[0].id, source="pixiv", source_asset_id="ready", ordinal=0),
                AssetSource(asset_id=assets[1].id, work_source_id=sources[0].id, source="pixiv", source_asset_id="pending", ordinal=1),
                AssetSource(asset_id=assets[2].id, work_source_id=sources[1].id, source="pixiv", source_asset_id="processing", ordinal=0),
                AssetSource(asset_id=assets[3].id, work_source_id=sources[1].id, source="pixiv", source_asset_id="failed", ordinal=1),
            ])
            db.add_all([
                MediaDerivativeOutbox(
                    asset_id=assets[0].id,
                    requested={"thumbnail": True},
                    state="complete",
                    attempts=1,
                    available_at=now - timedelta(minutes=20),
                    completed_at=now - timedelta(minutes=10),
                ),
                MediaDerivativeOutbox(
                    asset_id=assets[1].id,
                    requested={"thumbnail": True},
                    state="pending",
                    attempts=0,
                    available_at=now - timedelta(minutes=20),
                    created_at=now - timedelta(minutes=20),
                ),
                MediaDerivativeOutbox(
                    asset_id=assets[2].id,
                    requested={"thumbnail": True},
                    state="processing",
                    attempts=1,
                    available_at=now - timedelta(minutes=20),
                    lease_expires_at=now + timedelta(minutes=5),
                    created_at=now - timedelta(minutes=20),
                ),
                MediaDerivativeOutbox(
                    asset_id=assets[3].id,
                    requested={"thumbnail": True},
                    state="failed",
                    attempts=2,
                    available_at=now + timedelta(minutes=1),
                    last_error="fixture failure",
                    created_at=now - timedelta(minutes=20),
                ),
            ])
            # The outbox is unique per asset; fold the non-thumbnail control
            # onto a fifth asset so it cannot alter the thumbnail denominator.
            control = Asset(file_path="pixiv/progress/hash.jpg", file_name="hash.jpg", mime_type="image/jpeg")
            db.add(control)
            await db.flush()
            db.add(MediaDerivativeOutbox(
                asset_id=control.id,
                requested={"sha256": True},
                state="pending",
                attempts=0,
                available_at=now,
            ))
            await db.commit()

            progress = await reader(db, now=now, stall_after_seconds=300)

            assert progress == {
                "total": 4,
                "completed": 1,
                "pending": 1,
                "processing": 1,
                "failed": 1,
                "remaining": 3,
                "affected_works": 2,
                "completion_percent": 25.0,
                "status": "running",
                "last_completed_at": now - timedelta(minutes=10),
                "oldest_unfinished_at": now - timedelta(minutes=20),
                "stall_after_seconds": 300,
            }

            await db.execute(
                update(MediaDerivativeOutbox)
                .where(MediaDerivativeOutbox.asset_id == assets[2].id)
                .values(state="pending", lease_expires_at=None)
            )
            await db.commit()
            stalled = await reader(db, now=now, stall_after_seconds=300)
            assert stalled["status"] == "stalled"
            assert stalled["pending"] == 2
            assert stalled["processing"] == 0
    finally:
        async with async_session() as db:
            await _clear_tables(db)
        await engine.dispose()


def test_media_derivative_progress_route_is_static_and_typed():
    from app.main import app

    schema = app.openapi()
    operation = schema["paths"]["/api/v1/works/derivative-progress"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/MediaDerivativeProgressRead"
    )
    assert "created_at" in schema["components"]["schemas"]["WorkAssetRead"]["required"]
    assert "created_at" not in schema["components"]["schemas"]["MediaDerivativeProgressRead"]["properties"]
