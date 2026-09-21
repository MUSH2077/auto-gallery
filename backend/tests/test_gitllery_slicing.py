import uuid

import pytest
from sqlalchemy import event, text


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_preload_work_sources_scoped(tmp_path, monkeypatch):
    """preload_work_sources(work_ids=...) loads only the requested works —
    the status tail path must stay O(tail), not O(library)."""
    from app.database import async_session, engine
    from app.models import Creator, SourceCreator, Work, WorkSource, CurationChange
    from app.services.gitllery.slicing import RepoResolver

    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="七诗"); db.add(creator); await db.flush()
            db.add(SourceCreator(creator_id=creator.id, source="pixiv",
                                 source_creator_id="123", display_name="七诗"))
            w1 = Work(title="w1"); db.add(w1); await db.flush()
            db.add(WorkSource(work_id=w1.id, source="pixiv", source_work_id="9001",
                              source_creator_id="123", raw_metadata={}))
            w2 = Work(title="w2"); db.add(w2); await db.flush()
            db.add(WorkSource(work_id=w2.id, source="pixiv", source_work_id="9002",
                              source_creator_id="123", raw_metadata={}))
            await db.commit()

            def change(work_id):
                return CurationChange(commit_id=uuid.uuid4(), subject_type="work",
                                      subject_id=str(work_id), action="work_trashed",
                                      before_state=None, after_state={"visibility": "trashed"})

            r = RepoResolver(db)
            await r.preload_work_sources(work_ids=[str(w1.id)])
            assert len(await r.slice_changes([change(w1.id)])) == 1
            r2 = RepoResolver(db)
            await r2.preload_work_sources(work_ids=[])
            assert await r2.slice_changes([change(w1.id)]) == {}
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_repository_page_hydrates_a_bounded_page_without_per_key_queries():
    """A page costs a fixed number of queries even when every key is distinct."""
    from app.database import async_session, engine
    from app.models import (
        Creator,
        SourceCreator,
        Subscription,
        SubscriptionSource,
        Work,
        WorkSource,
    )
    from app.services.gitllery.slicing import RepoResolver

    observed: list[str] = []

    def record_query(_conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            observed.append(statement)

    try:
        async with async_session() as db:
            await _clear(db)
            creator = Creator(name="bounded-page")
            db.add(creator)
            await db.flush()
            subscription = Subscription(creator_id=creator.id, name="bounded-page")
            db.add(subscription)
            await db.flush()

            for index in range(8):
                source_creator_id = f"page-{index:02d}"
                db.add(
                    SourceCreator(
                        creator_id=creator.id,
                        source="pixiv",
                        source_creator_id=source_creator_id,
                        display_name=source_creator_id,
                    )
                )
                if index < 4:
                    db.add(
                        SubscriptionSource(
                            subscription_id=subscription.id,
                            source="pixiv",
                            source_creator_id=source_creator_id,
                            source_url=f"https://www.pixiv.net/users/{index}",
                        )
                    )
                work = Work(title=f"work-{index}")
                db.add(work)
                await db.flush()
                db.add(
                    WorkSource(
                        work_id=work.id,
                        source="pixiv",
                        source_work_id=f"work-{index}",
                        source_creator_id=source_creator_id,
                        raw_metadata={"user": {"id": source_creator_id}},
                    )
                )
            await db.commit()

            event.listen(engine.sync_engine, "before_cursor_execute", record_query)
            try:
                descriptors = await RepoResolver(db).repository_page(limit=25)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", record_query)

            assert len(descriptors) == 8
            assert len(observed) <= 5, "Repository page hydration must stay O(1) in page size"

            observed.clear()
            event.listen(engine.sync_engine, "before_cursor_execute", record_query)
            try:
                all_descriptors = await RepoResolver(db).all_repositories()
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", record_query)

            assert len(all_descriptors) == 8
            assert len(observed) <= 2, (
                "Full repository discovery must resolve representatives and "
                "identities in one bounded database read"
            )
            representative_queries = [
                statement
                for statement in observed
                if "DISTINCT ON (work_sources.source, work_sources.source_creator_id)"
                in statement
            ]
            assert len(representative_queries) == 1
            assert not any("row_number() OVER" in statement for statement in observed)
            assert " IN (" not in representative_queries[0], (
                "The all-repository path must not compare every WorkSource "
                "against an expanded repository-key list"
            )
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_repositories_chooses_one_deterministic_work_source_per_identity():
    from uuid import UUID

    from app.database import async_session, engine
    from app.models import Work, WorkSource
    from app.services.gitllery.slicing import RepoResolver

    try:
        async with async_session() as db:
            await _clear(db)
            work = Work(title="merged-work")
            db.add(work)
            await db.flush()
            # A merge may leave multiple provider works on one canonical work.
            # Insert the larger id first so heap order cannot masquerade as the
            # deterministic representative order.
            db.add_all(
                [
                    WorkSource(
                        id=UUID(int=2),
                        work_id=work.id,
                        source="pixiv",
                        source_work_id="high",
                        source_creator_id="same-creator",
                        raw_metadata={"id": "high"},
                    ),
                    WorkSource(
                        id=UUID(int=1),
                        work_id=work.id,
                        source="pixiv",
                        source_work_id="low",
                        source_creator_id="same-creator",
                        raw_metadata={"id": "low"},
                    ),
                ]
            )
            await db.commit()

            resolver = RepoResolver(db)
            resolver._gallerydl_config = {
                "extractor": {"pixiv": {"directory": ["pixiv", "{id}"]}}
            }
            descriptors = await resolver.all_repositories()

            assert len(descriptors) == 1
            assert descriptors[0].repository_id == "pixiv:same-creator"
            assert descriptors[0].creator_dir == "low"
    finally:
        async with async_session() as db:
            await _clear(db)
        await engine.dispose()
