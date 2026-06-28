"""One-off recovery for the 800-creator sync incident.

Fixes two data conditions left by the `downloaded -> complete` transition crash
(see app/models/task_state.py) and the creator auto-link gap:

  1. RELINK: SourceCreator rows with creator_id IS NULL whose identity matches a
     single-creator subscription (work is in the DB but the creator page is empty
     because creator -> source_creator -> work_source is broken). Links them to
     the subscription's creator. Identity is matched with the provider's own
     get_creator_dir_from_url, exactly like import_runner._should_auto_link_creator,
     so a bookmark/repost feed (ambiguous) is never mislinked.

  2. UNSTICK: download_jobs stuck in 'downloaded'. If the ledger shows the job has
     un-imported metadata (count>0) it is reported for manual import (not touched).
     If count==0 (nothing of its own to import) it is completed and its
     subscription_source last_synced_at is refreshed, releasing the
     'already_running' lock so the subscription resumes syncing.

Dry-run by default. Pass --apply to write. Safe to re-run.

    docker compose exec backend python recover_sync_state.py           # dry-run
    docker compose exec backend python recover_sync_state.py --apply
"""

import asyncio
import sys

from sqlalchemy import select

from app.database import async_session
from app.providers import init_providers, registry
from app.models.source_creator import SourceCreator
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.download_job import DownloadJob
from app.repositories.download_job import DownloadJobRepository
from app.services.artifact_ledger import ArtifactLedger
from app.services.subscription_enqueue import mark_source_sync_success

APPLY = "--apply" in sys.argv


async def relink_source_creators(db) -> tuple[int, int]:
    """Link unlinked SourceCreators to their subscription's creator by identity."""
    rows = (await db.execute(
        select(SourceCreator).where(SourceCreator.creator_id.is_(None))
    )).scalars().all()
    linked = skipped = 0
    for sc in rows:
        try:
            provider = registry.get(sc.source)
        except KeyError:
            skipped += 1
            continue
        # All subscription_sources of this source whose URL identity == this creator.
        ss_rows = (await db.execute(
            select(SubscriptionSource).where(SubscriptionSource.source == sc.source)
        )).scalars().all()
        candidate_creator_ids = set()
        for ss in ss_rows:
            if not ss.source_url:
                continue
            url_id = provider.get_creator_dir_from_url(ss.source_url)
            if url_id is not None and str(url_id) == str(sc.source_creator_id):
                sub = await db.get(Subscription, ss.subscription_id) if ss.subscription_id else None
                if sub and sub.creator_id:
                    candidate_creator_ids.add(sub.creator_id)
        if len(candidate_creator_ids) == 1:
            cid = next(iter(candidate_creator_ids))
            print(f"  RELINK source_creator {sc.source}/{sc.source_creator_id} "
                  f"({sc.display_name!r}) -> creator {cid}")
            if APPLY:
                sc.creator_id = cid
            linked += 1
        else:
            print(f"  SKIP   source_creator {sc.source}/{sc.source_creator_id} "
                  f"({sc.display_name!r}) -> {len(candidate_creator_ids)} candidate creators (manual review)")
            skipped += 1
    if APPLY:
        await db.flush()
    return linked, skipped


async def unstick_downloaded_jobs(db) -> tuple[int, int]:
    """Complete count==0 'downloaded' jobs; report count>0 ones for manual import."""
    jobs = (await db.execute(
        select(DownloadJob).where(DownloadJob.status == "downloaded")
    )).scalars().all()
    completed = needs_import = 0
    repo = DownloadJobRepository(db)
    for j in jobs:
        meta, img, _paths = await ArtifactLedger(db).counts(j.id)
        if meta > 0:
            print(f"  NEEDS-IMPORT download_job {str(j.id)[:8]} has {meta} un-imported "
                  f"metadata files — left for re-import (status unchanged)")
            needs_import += 1
            continue
        print(f"  COMPLETE download_job {str(j.id)[:8]} (count==0, nothing to import) "
              f"-> complete + release subscription")
        if APPLY:
            await repo.update_status(j, "complete", "Recovered: no new content (no-op re-sync)")
            if j.subscription_source_id:
                await mark_source_sync_success(db, j.subscription_source_id)
        completed += 1
    if APPLY:
        await db.flush()
    return completed, needs_import


async def main():
    init_providers()
    async with async_session() as db:
        print(f"=== recover_sync_state ({'APPLY' if APPLY else 'DRY-RUN'}) ===")
        print("\n[1] Relink unlinked source_creators:")
        linked, skipped = await relink_source_creators(db)
        print("\n[2] Unstick 'downloaded' jobs:")
        completed, needs_import = await unstick_downloaded_jobs(db)
        if APPLY:
            await db.commit()
        print("\n=== summary ===")
        print(f"  relinked: {linked}   skipped(ambiguous): {skipped}")
        print(f"  completed stuck jobs: {completed}   need manual import: {needs_import}")
        print("  (DRY-RUN — nothing written; re-run with --apply)" if not APPLY else "  (APPLIED)")


if __name__ == "__main__":
    asyncio.run(main())
