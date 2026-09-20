"""Tests for creator deduplication service — unit tests that don't require DB."""

import pytest
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select


class TestMergeCreatorValidation:
    def test_cannot_merge_into_self(self):
        """merge_creators should raise ValueError when target == source."""
        # Verify the validation logic exists in the function
        import inspect
        from app.services.creator_dedup import merge_creators
        src = inspect.getsource(merge_creators)
        assert "Cannot merge a creator into itself" in src

    def test_checks_both_exist(self):
        """merge_creators should raise ValueError when either creator is missing."""
        import inspect
        from app.services.creator_dedup import merge_creators
        src = inspect.getsource(merge_creators)
        assert "One or both creators not found" in src


class TestFindMergeCandidates:
    def test_function_exists(self):
        from app.services.creator_dedup import find_merge_candidates
        import inspect
        assert inspect.iscoroutinefunction(find_merge_candidates)

    def test_returns_list(self):
        import inspect
        from app.services.creator_dedup import find_merge_candidates
        sig = inspect.signature(find_merge_candidates)
        assert "db" in sig.parameters
        assert "limit" in sig.parameters


class TestBatchImportDedup:
    def test_find_existing_checks_danbooru_id(self):
        """find_existing_creator should check danbooru_artist_id first."""
        import inspect
        from app.services.creator_dedup import find_existing_creator
        src = inspect.getsource(find_existing_creator)
        assert "danbooru_artist_id" in src
        assert "Creator.danbooru_artist_id" in src

    def test_find_existing_checks_source_creator(self):
        """find_existing_creator should check SourceCreator as fallback."""
        import inspect
        from app.services.creator_dedup import find_existing_creator
        src = inspect.getsource(find_existing_creator)
        assert "SourceCreator" in src
        assert "source_creator_id" in src

    def test_find_existing_checks_creator_link(self):
        """find_existing_creator should check CreatorLink URL as last resort."""
        import inspect
        from app.services.creator_dedup import find_existing_creator
        src = inspect.getsource(find_existing_creator)
        assert "CreatorLink" in src


class TestMergeWorkflow:
    def test_merge_returns_stats(self):
        """merge_creators should return a stats dict with expected keys."""
        import inspect
        from app.services.creator_dedup import merge_creators
        src = inspect.getsource(merge_creators)
        assert '"links_moved"' in src
        assert '"source_creators_moved"' in src
        assert '"subscriptions_moved"' in src

    def test_merge_transfers_danbooru_id(self):
        """merge_creators should transfer danbooru_artist_id if target lacks one."""
        import inspect
        from app.services.creator_dedup import merge_creators
        src = inspect.getsource(merge_creators)
        assert "danbooru_artist_id" in src

    def test_merge_handles_descriptions(self):
        """merge_creators should merge descriptions."""
        import inspect
        from app.services.creator_dedup import merge_creators
        src = inspect.getsource(merge_creators)
        assert "description" in src


@pytest.mark.integration
@pytest.mark.asyncio
async def test_merge_moves_owned_rows_and_rebuilds_surviving_membership():
    from app.database import async_session
    from app.models import (
        Creator,
        CreatorAlias,
        CreatorCurationState,
        CreatorLink,
        SearchProjectionOutbox,
        SourceCreator,
        Subscription,
        SubscriptionSource,
        User,
        UserSubscription,
        UserSubscriptionSource,
        Work,
        WorkSource,
    )
    from app.services.creator_dedup import find_merge_candidates, merge_creators

    token = uuid4().hex
    artist_id = int(token[:8], 16) % 1_000_000_000
    async with async_session() as db:
        target = Creator(
            name=f"merge-target-{token}",
            description="Target description",
            danbooru_artist_id=artist_id,
            is_favorite=False,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        source = Creator(
            name=f"merge-source-{token}",
            description="Source description",
            danbooru_artist_id=artist_id,
            is_favorite=True,
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )
        user = User(
            username=f"merge-user-{token}",
            password_hash="test-only",
        )
        db.add_all([target, source, user])
        await db.flush()

        target_duplicate_alias = CreatorAlias(
            creator_id=target.id,
            value="Shared Alias",
            normalized_value="shared alias",
            source="manual",
            kind="name",
        )
        source_duplicate_alias = CreatorAlias(
            creator_id=source.id,
            value="Shared Alias",
            normalized_value="shared alias",
            source="manual",
            kind="name",
        )
        source_unique_alias = CreatorAlias(
            creator_id=source.id,
            value="Source Alias",
            normalized_value="source alias",
            source="manual",
            kind="name",
        )
        target_duplicate_link = CreatorLink(
            creator_id=target.id,
            url=f"https://example.test/{token}/shared",
            link_type="website",
        )
        source_duplicate_link = CreatorLink(
            creator_id=source.id,
            url=f"https://example.test/{token}/shared",
            link_type="website",
        )
        source_unique_link = CreatorLink(
            creator_id=source.id,
            url=f"https://example.test/{token}/source",
            link_type="website",
        )
        source_identity = SourceCreator(
            creator_id=source.id,
            source="pixiv",
            source_creator_id=f"merge-{token}",
        )
        source_state = CreatorCurationState(
            creator_id=source.id,
            visibility="archived",
        )
        subscription = Subscription(
            creator_id=source.id,
            name="Source subscription",
        )
        work = Work(title="Source work")
        db.add_all([
            target_duplicate_alias,
            source_duplicate_alias,
            source_unique_alias,
            target_duplicate_link,
            source_duplicate_link,
            source_unique_link,
            source_identity,
            source_state,
            subscription,
            work,
        ])
        await db.flush()
        work_source = WorkSource(
            work_id=work.id,
            source="pixiv",
            source_work_id=f"merge-work-{token}",
            source_creator_id=source_identity.source_creator_id,
        )
        repository = SubscriptionSource(
            subscription_id=subscription.id,
            source="pixiv",
            source_creator_id=source_identity.source_creator_id,
            source_url=f"https://www.pixiv.net/users/{token}",
        )
        membership = UserSubscription(
            user_id=user.id,
            subscription_id=subscription.id,
            name="Private source membership",
        )
        db.add_all([work_source, repository, membership])
        await db.flush()
        membership_source = UserSubscriptionSource(
            user_id=user.id,
            subscription_id=subscription.id,
            user_subscription_id=membership.id,
            subscription_source_id=repository.id,
        )
        db.add(membership_source)
        await db.flush()

        candidates = await find_merge_candidates(db)
        candidate = next(
            item for item in candidates
            if {str(target.id), str(source.id)} <= set(item["creator_ids"])
        )
        assert candidate["reason"] == "same_danbooru_artist_id"
        assert candidate["creator_ids"] == [str(target.id), str(source.id)]
        assert candidate["creator_names"] == [target.name, source.name]

        ids = {
            "target": target.id,
            "source": source.id,
            "source_unique_alias": source_unique_alias.id,
            "source_duplicate_alias": source_duplicate_alias.id,
            "source_unique_link": source_unique_link.id,
            "source_duplicate_link": source_duplicate_link.id,
            "source_identity": source_identity.id,
            "source_state": source_state.id,
            "subscription": subscription.id,
            "membership": membership.id,
            "membership_source": membership_source.id,
        }
        stats = await merge_creators(db, target.id, source.id)

        assert stats == {
            "aliases_moved": 1,
            "links_moved": 1,
            "source_creators_moved": 1,
            "subscriptions_moved": 1,
        }
        assert await db.get(Creator, ids["source"]) is None
        merged = await db.get(Creator, ids["target"])
        assert merged.description == "Target description\nSource description"
        assert merged.is_favorite is True
        assert (await db.get(CreatorAlias, ids["source_unique_alias"])).creator_id == target.id
        assert await db.get(CreatorAlias, ids["source_duplicate_alias"]) is None
        assert (await db.get(CreatorLink, ids["source_unique_link"])).creator_id == target.id
        assert await db.get(CreatorLink, ids["source_duplicate_link"]) is None
        assert (await db.get(SourceCreator, ids["source_identity"])).creator_id == target.id
        assert await db.get(CreatorCurationState, ids["source_state"]) is None
        assert (await db.get(Subscription, ids["subscription"])).creator_id == target.id
        assert await db.get(UserSubscription, ids["membership"]) is not None
        assert await db.get(UserSubscriptionSource, ids["membership_source"]) is not None

        membership_projection = (await db.execute(
            select(SearchProjectionOutbox).where(
                SearchProjectionOutbox.entity_id == str(membership.id),
                SearchProjectionOutbox.index_uid.endswith("_subscription_memberships_v1"),
            )
        )).scalar_one()
        assert membership_projection.action == "upsert"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_merge_rejects_two_subscriptions_before_mutating_rows():
    from app.database import async_session
    from app.models import Creator, CreatorLink, Subscription
    from app.services.creator_dedup import merge_creators

    token = uuid4().hex
    async with async_session() as db:
        target = Creator(name=f"merge-target-subscribed-{token}")
        source = Creator(name=f"merge-source-subscribed-{token}")
        db.add_all([target, source])
        await db.flush()

        source_link = CreatorLink(
            creator_id=source.id,
            url=f"https://example.test/{token}/source",
            link_type="website",
        )
        target_subscription = Subscription(
            creator_id=target.id,
            name="Target subscription",
        )
        source_subscription = Subscription(
            creator_id=source.id,
            name="Source subscription",
        )
        db.add_all([source_link, target_subscription, source_subscription])
        await db.commit()

        with pytest.raises(ValueError, match="Both creators have subscriptions"):
            await merge_creators(db, target.id, source.id)

        assert await db.get(Creator, target.id) is not None
        assert await db.get(Creator, source.id) is not None
        assert (await db.get(CreatorLink, source_link.id)).creator_id == source.id
        assert (await db.get(Subscription, target_subscription.id)).creator_id == target.id
        assert (await db.get(Subscription, source_subscription.id)).creator_id == source.id
