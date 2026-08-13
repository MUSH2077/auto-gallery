"""Regression coverage for upstream revisions of an existing work."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text


async def _clear(db):
    await db.execute(
        text(
            """
            TRUNCATE
                maintenance_audit_events,
                search_projection_outbox,
                search_index_states,
                import_curation_outbox,
                media_derivative_outbox,
                storage_artifacts,
                import_jobs,
                download_jobs,
                work_source_tags,
                work_tags,
                asset_sources,
                assets,
                work_sources,
                works,
                source_creators,
                subscription_sources,
                subscriptions,
                creators
            RESTART IDENTITY CASCADE
            """
        )
    )
    await db.commit()


def _pixiv_raw(page: int) -> dict:
    return {
        "id": 148166622,
        "title": "Updated two-page work",
        "description": "upstream metadata changed",
        "date": "2026-08-13T12:00:00+00:00",
        "page_count": 2,
        "num": page,
        "user": {"id": 12539859, "name": "Artist"},
        "tags": ["update", "two-pages"],
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_existing_work_metadata_and_new_page_are_idempotently_imported(
    tmp_path,
    monkeypatch,
):
    from app.config import settings
    from app.database import async_session, engine
    from app.jobs.import_runner import (
        _prepare_import_metadata,
        _update_existing_work_groups,
    )
    from app.models import (
        Asset,
        AssetSource,
        Creator,
        DownloadJob,
        MaintenanceAuditEvent,
        Subscription,
        SubscriptionSource,
        Work,
        WorkSource,
    )
    from app.providers import registry
    from app.services.artifact_discovery import group_metadata_by_work

    download_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    work_dir = download_root / "pixiv" / "12539859" / "148166622"
    work_dir.mkdir(parents=True)
    library_root.mkdir()
    monkeypatch.setattr(settings, "download_root", str(download_root))
    monkeypatch.setattr(settings, "library_root", str(library_root))

    p0_json = work_dir / "148166622_p0.json"
    p1_json = work_dir / "148166622_p1.json"
    p0_json.write_text(json.dumps(_pixiv_raw(0)), encoding="utf-8")
    p1_json.write_text(json.dumps(_pixiv_raw(1)), encoding="utf-8")
    (work_dir / "148166622_p0.jpg").write_bytes(b"old-page")
    (work_dir / "148166622_p1.jpg").write_bytes(b"new-page")

    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="existing-update", display_name="Artist")
            db.add(creator)
            await db.flush()
            subscription = Subscription(
                creator_id=creator.id,
                name="existing-update",
                schedule_mode=None,
                sync_enabled=True,
            )
            db.add(subscription)
            await db.flush()
            repository = SubscriptionSource(
                subscription_id=subscription.id,
                source="pixiv",
                source_creator_id="12539859",
                source_url="https://www.pixiv.net/users/12539859",
                is_enabled=True,
            )
            db.add(repository)
            await db.flush()
            download_job = DownloadJob(
                subscription_id=subscription.id,
                subscription_source_id=repository.id,
                source="pixiv",
                source_url=repository.source_url,
                status="complete",
            )
            db.add(download_job)
            work = Work(title="Old title")
            db.add(work)
            await db.flush()
            work_source = WorkSource(
                work_id=work.id,
                source="pixiv",
                source_work_id="148166622",
                source_creator_id="12539859",
                title="Old title",
                raw_metadata={"id": 148166622, "page_count": 1},
            )
            db.add(work_source)
            await db.flush()
            p0_asset = Asset(
                file_name="148166622_p0.jpg",
                file_path=str(
                    (work_dir / "148166622_p0.jpg").relative_to(download_root)
                ),
                file_size=8,
                mime_type="image/jpeg",
            )
            db.add(p0_asset)
            await db.flush()
            db.add(
                AssetSource(
                    asset_id=p0_asset.id,
                    work_source_id=work_source.id,
                    source="pixiv",
                    source_asset_id="148166622_p0",
                    ordinal=0,
                    role="page",
                )
            )
            await db.commit()
            await db.refresh(download_job)

        provider = registry.get("pixiv")
        groups, invalid = group_metadata_by_work(
            provider,
            [p0_json, p1_json],
        )
        assert invalid == []
        prepared, failures = await _prepare_import_metadata(
            provider,
            download_job,
            groups,
        )
        assert failures == {}

        first = await _update_existing_work_groups(
            provider,
            download_job,
            prepared,
        )
        second = await _update_existing_work_groups(
            provider,
            download_job,
            prepared,
        )

        assert first["updated"] == 1
        assert first["assets"] == 1
        assert first["failures"] == {}
        assert second["updated"] == 0
        assert second["unchanged"] == 1
        assert second["assets"] == 0

        async with async_session() as db:
            stored_source = (
                await db.execute(
                    select(WorkSource).where(
                        WorkSource.source == "pixiv",
                        WorkSource.source_work_id == "148166622",
                    )
                )
            ).scalar_one()
            assets = list(
                (
                    await db.execute(
                        select(AssetSource)
                        .where(AssetSource.work_source_id == stored_source.id)
                        .order_by(AssetSource.ordinal)
                    )
                ).scalars()
            )
            audit_count = await db.scalar(
                select(func.count(MaintenanceAuditEvent.id)).where(
                    MaintenanceAuditEvent.event_type
                    == "existing_work_upstream_update"
                )
            )

        assert stored_source.raw_metadata["page_count"] == 2
        assert [row.source_asset_id for row in assets] == [
            "148166622_p0",
            "148166622_p1",
        ]
        assert audit_count == 1
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
