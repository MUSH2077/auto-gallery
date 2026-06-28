import pytest

from app.services.creator_reconcile import reconcile_unlinked_source_creators


@pytest.mark.integration
@pytest.mark.asyncio
async def test_relinks_single_creator_subscription():
    from app.database import async_session
    from app.models.creator import Creator
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.models.source_creator import SourceCreator

    async with async_session() as db:
        creator = Creator(name="Machi", display_name="Machi")
        db.add(creator)
        await db.flush()
        sub = Subscription(creator_id=creator.id, name="Machi")
        db.add(sub)
        await db.flush()
        ss = SubscriptionSource(
            subscription_id=sub.id,
            source="pixiv",
            source_url="https://www.pixiv.net/users/4632856",
        )
        db.add(ss)
        sc = SourceCreator(
            source="pixiv",
            source_creator_id="4632856",
            display_name="Machi",
            creator_id=None,
        )
        db.add(sc)
        await db.flush()

        result = await reconcile_unlinked_source_creators(db, source="pixiv")
        await db.refresh(sc)

        assert result["linked"] >= 1
        assert sc.creator_id == creator.id
        await db.rollback()
