"""Private membership projections: real PostgreSQL and real Meili, no selected hits.

Run with the normal backend pytest isolation (test DB and generated index prefix).
The real integration test uses the native restartable delivery/rebuild engine.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import delete, select

from app.database import async_session
from app.models import Creator, CreatorAlias, Subscription, SubscriptionSource, User, UserSubscription, UserSubscriptionSource
from app.models.search_projection_outbox import SearchProjectionOutbox as Outbox
from app.models.search_delivery_receipt import SearchDeliveryReceipt as Receipt
from app.models.search_rebuild import SearchRebuild
from app.services.search import SearchService, MEMBERSHIPS_INDEX, SUBSCRIPTIONS_INDEX, INDEX_SETTINGS, SearchBackendUnavailable
from app.services.search_projection_outbox import request_search_projection, request_membership_projection_where
from app.services.subscription_membership import SubscriptionMembershipService
from tests.test_search_delivery import delivery, ready_receipt  # noqa: F401


@pytest.fixture
async def member_rows(delivery):
    async with async_session() as db:
        users = [User(username=f"membership-search-{uuid4().hex}", password_hash="test-only", permissions=["subscriptions"]) for _ in range(2)]
        db.add_all(users)
        creators = [Creator(name="Aurora Creator", display_name="Aurora Creator") for _ in range(62)]
        db.add_all(creators)
        await db.flush()
        subs = [Subscription(creator_id=creator.id, name="Canonical Observatory") for creator in creators]
        db.add_all(subs)
        await db.flush()
        stamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
        members = [UserSubscription(user_id=users[0].id, subscription_id=sub.id,
                                    name="星空收藏 StardustNotebook" if i == 0 else f"StardustNotebook {i}",
                                    created_at=stamp, updated_at=stamp) for i, sub in enumerate(subs)]
        other = UserSubscription(user_id=users[1].id, subscription_id=subs[0].id, name="雨夜书签 MoonlightJournal")
        db.add_all([*members, other])
        source = SubscriptionSource(subscription_id=subs[0].id, source="pixiv", source_creator_id="918273645",
                                    source_url="https://www.pixiv.net/users/918273645")
        db.add(source)
        await db.flush()
        db.add(UserSubscriptionSource(user_id=users[0].id, subscription_id=subs[0].id,
                                      user_subscription_id=members[0].id, subscription_source_id=source.id,
                                      last_synced_at=stamp))
        await db.commit()  # Deliberately pre-existing data: bootstrap must discover it.
        values = SimpleNamespace(users=[u.id for u in users], creators=[c.id for c in creators],
                                 subscriptions=[s.id for s in subs], memberships=[m.id for m in members],
                                 other=other.id, source=source.id)
    try:
        yield values
    finally:
        async with async_session() as db:
            await db.execute(delete(UserSubscriptionSource).where(UserSubscriptionSource.user_id.in_(values.users)))
            await db.execute(delete(UserSubscription).where(UserSubscription.user_id.in_(values.users)))
            await db.execute(delete(SubscriptionSource).where(SubscriptionSource.subscription_id.in_(values.subscriptions)))
            await db.execute(delete(Subscription).where(Subscription.id.in_(values.subscriptions)))
            await db.execute(delete(CreatorAlias).where(CreatorAlias.creator_id.in_(values.creators)))
            await db.execute(delete(Creator).where(Creator.id.in_(values.creators)))
            await db.execute(delete(User).where(User.id.in_(values.users)))
            await db.commit()


async def _search(actor, text, offset=0, limit=100):
    async with async_session() as db:
        return await SearchService(db).search(text, offset, limit, scope="subscriptions", permissions={"subscriptions"}, user_id=actor)


async def _deliver_version(delivery, identity, version):
    for _ in range(300):
        await delivery.run_delivery_slice()
        await ready_receipt()
        async with async_session() as db:
            row = (await db.execute(select(Outbox).where(Outbox.index_uid == MEMBERSHIPS_INDEX, Outbox.entity_id == str(identity)))).scalar_one()
            receipts = list((await db.execute(select(Receipt).where(Receipt.index_uid == MEMBERSHIPS_INDEX, Receipt.state == "complete"))).scalars())
            if row.completed_at is not None and row.version == version and any([str(row.id), version] in receipt.versions for receipt in receipts):
                return
        await asyncio.sleep(.03)
    pytest.fail(f"Membership {identity} version {version} never received its exact completed receipt")


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", [True], indirect=True)
async def test_real_membership_unicode_privacy_paging_rename_and_bootstrap_restart(delivery, member_rows, monkeypatch):
    from app.services import search_rebuild
    from app.config import settings
    rows = member_rows
    # Same-process slices re-read persisted state and resume the same build.
    # Actual process-restart recovery is a separate root-owned acceptance check.
    build_id = None
    for _ in range(350):
        await delivery.run_delivery_slice(limit=17)
        await ready_receipt()
        async with async_session() as db:
            build = (await db.execute(select(SearchRebuild).where(SearchRebuild.owner == search_rebuild.MEMBERSHIP_BOOTSTRAP_OWNER))).scalar_one()
            build_id = build_id or build.id
            assert build.id == build_id
            if build.state == "complete":
                assert build.progress["batches"] >= 4
                assert not (await db.execute(select(search_rebuild.membership_bootstrap_due_condition()))).scalar_one()
                break
        await asyncio.sleep(.03)
    else:
        pytest.fail("Membership upgrade bootstrap did not finish")

    for query in ("星空收藏", "星空", '"星空收藏"', '"星空收藏" Aurora', "StardustNote", "StardustNotebok"):
        first = await _search(rows.users[0], query)
        assert first["total"] >= 1, query
        assert str(rows.subscriptions[0]) in {item["id"] for item in first["subscriptions"]}, query
        assert (await _search(rows.users[1], query))["total"] == 0, query
    assert (await _search(rows.users[1], "MoonlightJournal"))["total"] == 1
    assert (await _search(rows.users[0], "MoonlightJournal"))["total"] == 0
    assert (await _search(rows.users[0], "星空收藏 missingunrelatedterm"))["total"] == 0
    for suffix in ("is:active -is:inactive", "source:pixiv", "source:pixiv source:x -source:bilibili", f"repo:{rows.source}",
                   "uid:pixiv/918273645", 'url:"https://www.pixiv.net/users/918273645"', "has:last-sync"):
        response = await _search(rows.users[0], "星空收藏 " + suffix)
        assert response["total"] == 1, suffix
    from app.services.search_language import SearchQueryError
    with pytest.raises(SearchQueryError) as conflict:
        await _search(rows.users[0], "星空收藏 source:pixiv source:x -source:x")
    assert conflict.value.diagnostic.code == "conflicting_values"
    for sort in ("relevance", "name-asc", "name-desc", "created-asc", "created-desc", "updated-asc", "updated-desc", "last-sync-asc", "last-sync-desc"):
        query = f"StardustNotebook sort:{sort}"
        first, second = await _search(rows.users[0], query, 0, 50), await _search(rows.users[0], query, 50, 50)
        assert first["total"] == second["total"] == 62
        ids = [item["id"] for item in first["subscriptions"] + second["subscriptions"]]
        assert len(ids) == len(set(ids)) == 62
        assert ids == [item["id"] for item in (await _search(rows.users[0], query))["subscriptions"]]
        # Non-page-aligned offsets remain bounded and exact.
        assert [item["id"] for item in (await _search(rows.users[0], query, 47, 9))["subscriptions"]] == ids[47:56]
        assert not any(key in item for item in first["subscriptions"] for key in ("user_id", "subscription_id", "membership_id", "canonical_name", "projection_hash"))

    # Existing Meili ranking rules can rank word position before explicit sort:
    # StardustNotebook follows Chinese text in one private name. Assert UUID
    # tiebreaks only when every document matches identical canonical attributes.
    for direction in ("asc", "desc"):
        result = await _search(rows.users[0], f"Canonical Observatory sort:name-{direction}")
        assert result["total"] == 62
        assert [item["id"] for item in result["subscriptions"]] == sorted(
            map(str, rows.subscriptions), reverse=direction == "desc",
        )

    no_inline = AsyncMock(side_effect=AssertionError("business mutations must never drain Meili"))
    monkeypatch.setattr(SearchService, "drain_search_projection_outbox", no_inline)
    async with async_session() as db:
        service = SubscriptionMembershipService(db, rows.users[0])
        await service.update(rows.subscriptions[0], {"name": "雪山收藏 RenamedNotebook"})
        await db.commit()
        assert (await service.get(rows.subscriptions[0])).name == "雪山收藏 RenamedNotebook"
        event = (await db.execute(select(Outbox).where(Outbox.index_uid == MEMBERSHIPS_INDEX, Outbox.entity_id == str(rows.memberships[0])))).scalar_one()
        version = event.version
        assert event.completed_at is None
    no_inline.assert_not_called()
    await _deliver_version(delivery, rows.memberships[0], version)
    assert (await _search(rows.users[0], "雪山收藏"))["total"] == 1
    assert (await _search(rows.users[0], "星空收藏"))["total"] == 0
    async with async_session() as db:
        await SubscriptionMembershipService(db, rows.users[0]).remove(rows.subscriptions[0])
        await db.commit()
        event = (await db.execute(select(Outbox).where(Outbox.index_uid == MEMBERSHIPS_INDEX, Outbox.entity_id == str(rows.memberships[0])))).scalar_one()
        version = event.version
        assert event.action == "delete"
    # Live ownership check removes stale hits before tombstone delivery.
    assert (await _search(rows.users[0], "雪山收藏"))["subscriptions"] == []
    await _deliver_version(delivery, rows.memberships[0], version)
    assert (await _search(rows.users[0], "雪山收藏"))["total"] == 0
    assert (await _search(rows.users[1], "MoonlightJournal"))["total"] == 1
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{settings.meili_url}/indexes/{MEMBERSHIPS_INDEX}/documents/{rows.memberships[0]}", headers={"Authorization": f"Bearer {settings.meili_master_key}"})
        assert response.status_code == 404

    async with async_session() as db:
        service = SubscriptionMembershipService(db, rows.users[0])
        joined = await service.create_or_join({"creator_id": rows.creators[0], "name": "重逢 RendezvousJournal"})
        await db.commit()
        assert (await service.get(rows.subscriptions[0])).name == "重逢 RendezvousJournal"
        membership = (await db.execute(select(UserSubscription).where(UserSubscription.user_id == rows.users[0], UserSubscription.subscription_id == rows.subscriptions[0]))).scalar_one()
        joined_id = membership.id
        event = (await db.execute(select(Outbox).where(Outbox.index_uid == MEMBERSHIPS_INDEX, Outbox.entity_id == str(joined_id)))).scalar_one()
        joined_version = event.version
    assert joined.id == rows.subscriptions[0]
    no_inline.assert_not_called()
    await _deliver_version(delivery, joined_id, joined_version)
    assert (await _search(rows.users[0], "RendezvousJournal"))["total"] == 1
    assert (await _search(rows.users[1], "RendezvousJournal"))["total"] == 0


@pytest.mark.asyncio
async def test_membership_projection_rollback_fanout_and_hashes(member_rows, monkeypatch):
    rows = member_rows
    async with async_session() as db:
        before = await SearchService(db)._build_membership_documents([rows.memberships[0], rows.other])
        assert {doc["user_id"] for doc in before} == set(rows.users)
        assert next(doc for doc in before if doc["id"] == str(rows.other))["last_synced_at"] is None
        assert "MoonlightJournal" not in str(await SearchService(db)._build_subscription_documents([rows.subscriptions[0]]))
        member = await db.get(UserSubscription, rows.memberships[0])
        member.name = "RollbackLabel"
        await request_search_projection(db, membership_ids=[member.id])
        changed = await SearchService(db)._build_membership_documents([member.id])
        assert changed[0]["projection_hash"] != next(doc["projection_hash"] for doc in before if doc["id"] == str(member.id))
        await db.rollback()
        assert not list((await db.execute(select(Outbox).where(Outbox.index_uid == MEMBERSHIPS_INDEX))).scalars())
        for kwargs in ({"subscription_ids": [rows.subscriptions[0]]}, {"creator_ids": [rows.creators[0]]}, {"repository_ids": [rows.source]}):
            await request_search_projection(db, **kwargs)
            assert {event.entity_id for event in (await db.execute(select(Outbox).where(Outbox.index_uid == MEMBERSHIPS_INDEX))).scalars()} == {str(rows.memberships[0]), str(rows.other)}
            await db.rollback()
        from app.services.creator_aliases import observe_creator_aliases, AliasObservation
        await observe_creator_aliases(db, rows.creators[0], [AliasObservation(source="pixiv", kind="name", value="PrivateProjectionAlias")])
        assert {event.entity_id for event in (await db.execute(select(Outbox).where(Outbox.index_uid == MEMBERSHIPS_INDEX))).scalars()} == {str(rows.memberships[0]), str(rows.other)}
        changed = await SearchService(db)._build_membership_documents([rows.memberships[0]])
        assert "PrivateProjectionAlias" in str(changed[0])
        await db.rollback()
        await request_search_projection(db, subscription_ids=[rows.subscriptions[0]], deleted_membership_ids=[rows.memberships[0]])
        event = (await db.execute(select(Outbox).where(Outbox.index_uid == MEMBERSHIPS_INDEX, Outbox.entity_id == str(rows.memberships[0])))).scalar_one()
        assert event.action == "delete"
        await db.rollback()
        # Exercise actual keyset progression without hundreds of fixture users.
        import app.services.search_projection_outbox as projection
        monkeypatch.setattr(projection, "OUTBOX_SQL_BATCH_SIZE", 7)
        await request_membership_projection_where(db, UserSubscription.user_id == rows.users[0], deleting=True)
        events = list((await db.execute(select(Outbox).where(Outbox.index_uid == MEMBERSHIPS_INDEX))).scalars())
        assert len(events) == 62 and {event.action for event in events} == {"delete"}
        await db.rollback()


@pytest.mark.asyncio
async def test_subscription_meili_failure_preserves_structured_browse(member_rows, monkeypatch):
    import app.services.search as search
    def unavailable(**kwargs):
        raise RuntimeError("isolated Meili unavailable")
    monkeypatch.setattr(search, "_client", unavailable)
    with pytest.raises(SearchBackendUnavailable):
        await _search(member_rows.users[0], "StardustNotebook")
    result = await _search(member_rows.users[0], "sort:name-asc")
    assert result["total"] == 62
    assert {item["id"] for item in result["subscriptions"]} == set(map(str, member_rows.subscriptions))
    assert INDEX_SETTINGS[MEMBERSHIPS_INDEX]["pagination"] == INDEX_SETTINGS[SUBSCRIPTIONS_INDEX]["pagination"]


@pytest.mark.asyncio
async def test_actor_filter_precedes_exact_count_and_page_and_permissions(member_rows, monkeypatch):
    import app.services.search as search
    from app.services.search import SearchPermissionError
    calls = []
    async with async_session() as db:
        doc = (await SearchService(db)._build_membership_documents([member_rows.memberships[0]]))[0]
    class Client:
        def multi_search(self, params):
            calls.extend(params)
            return [SimpleNamespace(hits=[dict(doc)]), SimpleNamespace(total_hits=1)]
    monkeypatch.setattr(search, "_client", lambda **kwargs: Client())
    result = await _search(member_rows.users[0], "StardustNotebook", 17, 9)
    assert result["total"] == 1
    assert len(calls) == 2
    for params in calls:
        assert params.index_uid == MEMBERSHIPS_INDEX
        assert params.filter == f"user_id = {member_rows.users[0]}"
        assert params.matching_strategy == "all"
    assert calls[0].offset == 17 and calls[0].limit == 9
    assert calls[1].page == 1 and calls[1].hits_per_page == 1
    assert result["subscriptions"][0]["id"] == str(member_rows.subscriptions[0])
    calls.clear()
    async with async_session() as db:
        with pytest.raises(SearchPermissionError):
            await SearchService(db).search("StardustNotebook", scope="subscriptions", permissions={"library"}, user_id=member_rows.users[0])
    assert calls == []


@pytest.mark.asyncio
async def test_bootstrap_failed_attempt_has_persisted_backoff(delivery):
    from datetime import timedelta
    from app.services import search_rebuild
    from app.services.search_delivery import now
    async with async_session() as db:
        assert (await db.execute(select(search_rebuild.membership_bootstrap_due_condition()))).scalar_one()
        build = SearchRebuild(owner=search_rebuild.MEMBERSHIP_BOOTSTRAP_OWNER, state="failed", phase="complete", progress={"indexes": [MEMBERSHIPS_INDEX]})
        db.add(build)
        await db.flush()
        assert not (await db.execute(select(search_rebuild.membership_bootstrap_due_condition()))).scalar_one()
        build.updated_at = now() - timedelta(minutes=6)
        await db.flush()
        assert (await db.execute(select(search_rebuild.membership_bootstrap_due_condition()))).scalar_one()
        build.state = "complete"
        await db.flush()
        assert not (await db.execute(select(search_rebuild.membership_bootstrap_due_condition()))).scalar_one()
        await db.rollback()


@pytest.mark.asyncio
async def test_native_restricted_user_delete_rolls_back_projection_and_domain(member_rows):
    from sqlalchemy.exc import IntegrityError
    from app.models import SearchIndexState
    from app.services.users import UserService

    rows = member_rows
    async with async_session() as db:
        # Preserve an existing intent/version and generation, so rollback must
        # undo both coalesced updates and newly introduced delete intents.
        await request_search_projection(db, membership_ids=[rows.memberships[0]])
        await db.commit()

        async def snapshot():
            selections = {
                "users": (User, User.id.in_(rows.users)),
                "memberships": (UserSubscription, UserSubscription.user_id.in_(rows.users)),
                "bindings": (UserSubscriptionSource, UserSubscriptionSource.user_id.in_(rows.users)),
                "subscriptions": (Subscription, Subscription.id.in_(rows.subscriptions)),
                "sources": (SubscriptionSource, SubscriptionSource.subscription_id.in_(rows.subscriptions)),
                "creators": (Creator, Creator.id.in_(rows.creators)),
                "outbox": (Outbox, True),
                "index_states": (SearchIndexState, True),
            }
            values = {}
            for label, (model, condition) in selections.items():
                result = await db.execute(select(model.__table__).where(condition).order_by(model.id))
                values[label] = [dict(row) for row in result.mappings()]
            return values

        before = await snapshot()
        assert len(before["memberships"]) == 63
        assert len(before["outbox"]) == 1
        assert before["outbox"][0]["action"] == "upsert"
        assert before["outbox"][0]["version"] == 1
        assert before["index_states"][0]["database_generation"] > 0
        # Native deletion requests tombstones before the populated ownership
        # relationship rejects deletion. Do not bypass the business service.
        with pytest.raises(IntegrityError):
            await UserService(db).delete(rows.users[0])
        await db.rollback()
        assert await snapshot() == before
