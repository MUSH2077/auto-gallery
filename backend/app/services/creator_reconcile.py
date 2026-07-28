"""Link orphaned SourceCreators (creator_id IS NULL) to their subscription's
creator by identity match. Mirrors import_runner._should_auto_link_creator so a
bookmark/repost feed (ambiguous identity) is never mislinked."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source_creator import SourceCreator
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.providers import registry


async def reconcile_unlinked_source_creators(db: AsyncSession, source: str | None = None) -> dict:
    """Link unlinked SourceCreators to the unique creator of a single-creator
    subscription whose URL identity equals the source_creator_id.

    Returns {"linked": int, "skipped": int}. Flushes on link; never commits
    (the caller owns the transaction).
    """
    q = select(SourceCreator).where(SourceCreator.creator_id.is_(None))
    if source:
        q = q.where(SourceCreator.source == source)
    unlinked = (await db.execute(q)).scalars().all()

    linked = skipped = 0
    for sc in unlinked:
        try:
            provider = registry.get(sc.source)
        except KeyError:
            skipped += 1
            continue
        ss_rows = (await db.execute(
            select(SubscriptionSource).where(SubscriptionSource.source == sc.source)
        )).scalars().all()
        candidates: set = set()
        for ss in ss_rows:
            if not ss.source_url or not ss.subscription_id:
                continue
            url_id = provider.get_creator_dir_from_url(ss.source_url)
            if url_id is not None and str(url_id) == str(sc.source_creator_id):
                sub = await db.get(Subscription, ss.subscription_id)
                if sub and sub.creator_id:
                    candidates.add(sub.creator_id)
        if len(candidates) == 1:
            sc.creator_id = next(iter(candidates))
            linked += 1
        else:
            skipped += 1
    if linked:
        await db.flush()
    return {"linked": linked, "skipped": skipped}
