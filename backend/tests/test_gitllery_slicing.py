import uuid

import pytest
from sqlalchemy import text


async def _clear(db):
    await db.execute(text(
        "TRUNCATE curation_changes, curation_commits, asset_sources, assets, "
        "work_sources, works, source_creators, subscription_sources, subscriptions, "
        "creators RESTART IDENTITY CASCADE"))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slice_work_change_to_single_repo():
    from app.database import async_session, engine
    from app.models import (Creator, SourceCreator, Work, WorkSource,
                             CurationChange)
    from app.services.gitllery.slicing import RepoResolver

    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗")
            db.add(creator)
            await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work = Work(title="w")
            db.add(work)
            await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={"user": {"name": "七诗"}}))
            await db.commit()

            change = CurationChange(commit_id=uuid.uuid4(), subject_type="work",
                                    subject_id=str(work.id), action="work_trashed",
                                    before_state=None, after_state={"visibility": "trashed"})
            sliced = await RepoResolver(db).slice_changes([change])
            assert len(sliced) == 1
            desc, changes = next(iter(sliced.values()))
            assert desc.source == "pixiv"
            assert desc.source_creator_id == "123"
            assert desc.creator_id == str(creator.id)
            assert len(changes) == 1
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slice_creator_change_fans_out_to_all_source_repos():
    from app.database import async_session, engine
    from app.models import Creator, SourceCreator, CurationChange
    from app.services.gitllery.slicing import RepoResolver

    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗")
            db.add(creator)
            await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            db.add(SourceCreator(creator_id=creator.id, source="x",
                                 source_creator_id="abc", display_name="七诗"))
            await db.commit()

            change = CurationChange(commit_id=uuid.uuid4(), subject_type="creator",
                                    subject_id=str(creator.id), action="creator_archived",
                                    before_state=None, after_state={"visibility": "archived"})
            sliced = await RepoResolver(db).slice_changes([change])
            sources = sorted(desc.source for desc, _ in sliced.values())
            assert sources == ["pixiv", "x"]
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_slice_repository_change_skips_source_without_downloads():
    """A repository (subscription_source) with no WorkSource must not produce a
    descriptor — otherwise undownloaded/reference sources collapse into a bogus
    {source}/unknown/.gitllery."""
    from app.database import async_session, engine
    from app.models import Creator, Subscription, SubscriptionSource, CurationChange
    from app.services.gitllery.slicing import RepoResolver

    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗")
            db.add(creator)
            await db.flush()
            sub = Subscription(creator_id=creator.id, name="七诗")
            db.add(sub)
            await db.flush()
            # Subscription source with NO work_sources / library files.
            ss = SubscriptionSource(subscription_id=sub.id, source="danbooru",
                                    source_creator_id="yosei_bin",
                                    source_url="https://danbooru.donmai.us/posts?tags=yosei_bin")
            db.add(ss)
            await db.flush()
            await db.commit()

            change = CurationChange(commit_id=uuid.uuid4(), subject_type="repository",
                                    subject_id=str(ss.id), action="repository_added",
                                    before_state=None, after_state={"id": str(ss.id)})
            sliced = await RepoResolver(db).slice_changes([change])
            assert sliced == {}
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repos_for_change_is_cached_per_subject():
    """The same (subject_type, subject_id) is resolved against the DB once, even
    across many changes — kills O(commits x changes) query amplification."""
    from app.database import async_session, engine
    from app.models import Creator, SourceCreator, Work, WorkSource, CurationChange
    from app.services.gitllery.slicing import RepoResolver

    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗")
            db.add(creator)
            await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            work = Work(title="w")
            db.add(work)
            await db.flush()
            db.add(WorkSource(work_id=work.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={"user": {"name": "七诗"}}))
            await db.commit()

            resolver = RepoResolver(db)
            calls = {"n": 0}
            original = resolver._resolve_repos_for_change

            async def counting(change):
                calls["n"] += 1
                return await original(change)
            resolver._resolve_repos_for_change = counting

            changes = [
                CurationChange(commit_id=uuid.uuid4(), subject_type="work",
                               subject_id=str(work.id), action=a,
                               before_state=None, after_state={"visibility": v})
                for a, v in [("work_trashed", "trashed"), ("work_restored", "visible"),
                             ("work_favorited", "visible")]
            ]
            sliced = await resolver.slice_changes(changes)
            assert calls["n"] == 1
            assert len(sliced) == 1
            desc, _ = next(iter(sliced.values()))
            assert desc.source == "pixiv"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
