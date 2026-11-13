"""Projection dependency scope and bounded membership fanout on PostgreSQL."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.database import async_session, engine
from app.models import (
    Creator, SourceCreator, Subscription, SubscriptionSource, User,
    UserSubscription, Work, WorkSource,
)
from app.models.search_projection_outbox import SearchProjectionOutbox as Outbox
from app.services.creator import CreatorService
from app.services import search_projection_outbox as projection

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def graph():
    async with async_session() as db:
        creators = [Creator(name=f"projection-{uuid4()}") for _ in range(3)]
        users = [User(username=f"projection-{uuid4()}", password_hash="test-only") for _ in range(2)]
        works = [Work(title=f"projection-{uuid4()}") for _ in range(4)]
        db.add_all([*creators, *users, *works])
        await db.flush()
        subs = [Subscription(creator_id=c.id) for c in creators]
        db.add_all(subs)
        await db.flush()
        repositories = [
            SubscriptionSource(subscription_id=subs[0].id, source="pixiv", source_creator_id="projection-identity"),
            SubscriptionSource(subscription_id=subs[0].id, source="danbooru", source_creator_id="projection-repository"),
            SubscriptionSource(subscription_id=subs[0].id, source="x", source_url="https://example.test/Projection"),
        ]
        db.add_all([
            *repositories,
            SourceCreator(creator_id=creators[0].id, source="pixiv", source_creator_id="projection-identity"),
            SourceCreator(creator_id=creators[1].id, source="pixiv", source_creator_id="projection-other"),
            WorkSource(work_id=works[0].id, source="pixiv", source_work_id=str(uuid4()), source_creator_id="projection-identity"),
            WorkSource(work_id=works[1].id, source="danbooru", source_work_id=str(uuid4()), source_creator_id="projection-repository"),
            WorkSource(work_id=works[2].id, source="manual", source_work_id=str(uuid4()), source_url=" HTTPS://EXAMPLE.TEST/projection/ "),
            # A second route to the same work must not expand the dependency set.
            WorkSource(work_id=works[0].id, source="manual", source_work_id=str(uuid4()), source_url="https://example.test/Projection/"),
            WorkSource(work_id=works[3].id, source="pixiv", source_work_id=str(uuid4()), source_creator_id="projection-other"),
        ])
        members = [
            UserSubscription(user_id=users[0].id, subscription_id=subs[0].id),
            UserSubscription(user_id=users[1].id, subscription_id=subs[0].id),
            UserSubscription(user_id=users[0].id, subscription_id=subs[1].id),
            UserSubscription(user_id=users[0].id, subscription_id=subs[2].id),
        ]
        db.add_all(members)
        await db.flush()
        yield SimpleNamespace(db=db, creators=creators, works=works, subs=subs,
                              repositories=repositories, members=members)
        await db.rollback()
    await engine.dispose()


async def membership_events(graph):
    rows = (await graph.db.execute(
        select(Outbox.entity_id, Outbox.action, Outbox.version)
        .where(Outbox.index_uid == projection.DEFAULT_MEMBERSHIPS_INDEX_UID,
               Outbox.entity_id.in_([str(m.id) for m in graph.members]))
    )).all()
    return {identity: (action, version) for identity, action, version in rows}


async def test_creator_dependencies_preserve_both_work_routes_and_scope(graph):
    result = await CreatorService(graph.db)._projection_dependents(graph.creators[0].id)
    assert result == (
        {w.id for w in graph.works[:3]},
        {graph.subs[0].id},
        {r.id for r in graph.repositories},
    )


async def test_creator_dependencies_include_subscription_without_repository(graph):
    result = await CreatorService(graph.db)._projection_dependents(graph.creators[1].id)
    assert result == ({graph.works[3].id}, {graph.subs[1].id}, set())


async def test_creator_dependencies_see_new_relationship_in_same_transaction(graph):
    service = CreatorService(graph.db)
    await service._projection_dependents(graph.creators[0].id)
    work = Work(title="new dependency")
    graph.db.add(work)
    await graph.db.flush()
    repository = SubscriptionSource(subscription_id=graph.subs[0].id, source="x",
                                    source_creator_id=str(uuid4()))
    graph.db.add_all([
        repository,
        WorkSource(work_id=work.id, source="x", source_work_id=str(uuid4()),
                   source_creator_id=repository.source_creator_id),
    ])
    await graph.db.flush()
    work_ids, _, repository_ids = await service._projection_dependents(graph.creators[0].id)
    assert work.id in work_ids and repository.id in repository_ids


async def test_creator_deletion_uses_dependency_snapshot_after_relationships_removed(graph):
    service = CreatorService(graph.db)
    creator_id = graph.creators[0].id
    snapshot = await service._projection_dependents(creator_id)
    await projection.request_membership_projection_where(
        graph.db, UserSubscription.subscription_id == graph.subs[0].id, deleting=True,
    )
    await graph.db.execute(delete(UserSubscription).where(UserSubscription.subscription_id == graph.subs[0].id))
    await graph.db.execute(delete(SubscriptionSource).where(SubscriptionSource.subscription_id == graph.subs[0].id))
    await graph.db.execute(delete(Subscription).where(Subscription.id == graph.subs[0].id))
    await graph.db.execute(delete(SourceCreator).where(SourceCreator.creator_id == creator_id))
    await graph.db.execute(delete(Creator).where(Creator.id == creator_id))
    await service._request_creator_projection(creator_id, deleting=True, dependents=snapshot)
    expected = {
        (projection.DEFAULT_WORKS_INDEX_UID, str(w.id), "upsert") for w in graph.works[:3]
    } | {
        (projection.DEFAULT_CREATORS_INDEX_UID, str(creator_id), "delete"),
        (projection.DEFAULT_SUBSCRIPTIONS_INDEX_UID, str(graph.subs[0].id), "delete"),
    } | {
        (projection.DEFAULT_REPOSITORIES_INDEX_UID, str(r.id), "delete") for r in graph.repositories
    }
    events = set((await graph.db.execute(select(Outbox.index_uid, Outbox.entity_id, Outbox.action))).all())
    assert expected <= events


async def test_overlapping_membership_routes_enqueue_once_but_new_request_increments_version(graph):
    kwargs = {
        "subscription_ids": (v for v in [graph.subs[0].id, str(graph.subs[0].id)]),
        "creator_ids": [graph.creators[0].id],
        "repository_ids": [r.id for r in graph.repositories],
    }
    await projection.request_search_projection(graph.db, **kwargs)
    expected = {str(m.id): ("upsert", 1) for m in graph.members[:2]}
    assert await membership_events(graph) == expected
    await projection.request_search_projection(graph.db, creator_ids=[graph.creators[0].id])
    assert await membership_events(graph) == {identity: ("upsert", 2) for identity in expected}


async def test_membership_fanout_keeps_large_inputs_and_keyset_paging_bounded(graph, monkeypatch):
    monkeypatch.setattr(projection, "OUTBOX_SQL_BATCH_SIZE", 2)
    unrelated = [uuid4() for _ in range(601)]
    await projection.request_search_projection(
        graph.db,
        subscription_ids=[*unrelated, graph.subs[0].id],
        creator_ids=[*unrelated, graph.creators[1].id],
        repository_ids=[*unrelated, graph.repositories[0].id],
    )
    assert await membership_events(graph) == {str(m.id): ("upsert", 1) for m in graph.members[:3]}


async def test_membership_fanout_preserves_delete_precedence(graph):
    await projection.request_search_projection(
        graph.db, creator_ids=[graph.creators[0].id],
        deleted_subscription_ids=[graph.subs[0].id],
    )
    assert await membership_events(graph) == {str(m.id): ("delete", 2) for m in graph.members[:2]}


async def test_membership_fanout_savepoint_rollback_leaves_no_intent(graph):
    async with graph.db.begin_nested() as nested:
        await projection.request_search_projection(graph.db, creator_ids=[graph.creators[0].id])
        assert await membership_events(graph)
        await nested.rollback()
    assert await membership_events(graph) == {}
