from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bounded_import_children_finalize_shared_parent_only_after_last_batch():
    """A completed 25-work child must not terminalize a parent with siblings."""
    from app.database import async_session, engine
    from app.models.creator import Creator
    from app.models.download_job import DownloadJob
    from app.models.import_job import ImportJob
    from app.models.subscription import Subscription
    from app.services.import_lifecycle import (
        close_bounded_import_publication,
        coordinate_import_parent_completion,
    )

    async def clear(db):
        await db.execute(text("""
            TRUNCATE
                task_events,
                task_runs,
                import_jobs,
                download_jobs,
                subscriptions,
                creators
            RESTART IDENTITY CASCADE
        """))
        await db.commit()

    try:
        async with async_session() as db:
            await clear(db)
            creator = Creator(name="bounded-parent")
            db.add(creator)
            await db.flush()
            subscription = Subscription(
                creator_id=creator.id,
                name="Bounded parent",
            )
            db.add(subscription)
            await db.flush()
            parent = DownloadJob(
                subscription_id=subscription.id,
                source="pixiv",
                source_url="https://www.pixiv.net/users/1980643",
                status="importing",
                manifest={
                    "disk_import_recovery": True,
                    "bounded_import_publication_open": True,
                },
            )
            db.add(parent)
            await db.flush()
            first = ImportJob(download_job_id=parent.id, status="complete")
            second = ImportJob(download_job_id=parent.id, status="running")
            db.add_all([first, second])
            await db.flush()

            first_result = await coordinate_import_parent_completion(
                db,
                first,
                status="complete",
                stats={
                    "works": 25,
                    "assets": 25,
                    "multi_page": 0,
                    "skipped": 0,
                    "existing": 0,
                },
                total_groups=25,
                message="Imported 25 works",
            )
            assert first_result.should_finalize is False
            assert first_result.parent.status == "importing"
            await db.commit()

            second.status = "complete"
            second_result = await coordinate_import_parent_completion(
                db,
                second,
                status="complete",
                stats={
                    "works": 1,
                    "assets": 2,
                    "multi_page": 1,
                    "skipped": 0,
                    "existing": 0,
                },
                total_groups=1,
                message="Imported 1 work",
            )
            assert second_result.should_finalize is False

            final_result = await close_bounded_import_publication(db, parent.id)
            assert final_result is not None
            assert final_result.should_finalize is True
            assert final_result.status == "complete"
            assert final_result.total_groups == 26
            assert final_result.stats == {
                "works": 26,
                "assets": 27,
                "multi_page": 1,
                "skipped": 0,
                "existing": 0,
            }
    finally:
        async with async_session() as db:
            await clear(db)
        await engine.dispose()
