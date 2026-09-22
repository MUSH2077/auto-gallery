"""RQ boundary for daily Pixiv ranking snapshots and heat projection."""

from __future__ import annotations

import asyncio
from datetime import date

from sqlalchemy import func, select

from app.database import async_session
from app.models.remote_discovery import RemoteAccount
from app.remote_discovery.pixiv import PIXIV_RANKING_MODES, PixivRemoteDiscoveryAdapter
from app.remote_discovery.registry import registry
from app.services.pixiv_ranking_scheduler import pixiv_ranking_plan
from app.services.pixiv_ranking_sync import (
    PixivRankingDateNotReady,
    store_pixiv_ranking_results,
)
from app.services.remote_accounts import RemoteAccountService


async def _healthy_pixiv_account(db) -> RemoteAccount | None:
    return (
        await db.execute(
            select(RemoteAccount)
            .where(
                RemoteAccount.source == "pixiv",
                RemoteAccount.is_enabled.is_(True),
                RemoteAccount.credential_ciphertext.is_not(None),
                func.lower(func.coalesce(RemoteAccount.auth_status, "")).in_(
                    ("healthy", "ok", "success")
                ),
            )
            .order_by(RemoteAccount.last_authenticated_at.desc().nullslast(), RemoteAccount.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def sync_pixiv_rankings_async(ranking_date_iso: str | None = None) -> dict:
    ranking_date = (
        date.fromisoformat(ranking_date_iso)
        if ranking_date_iso
        else pixiv_ranking_plan().ranking_date
    )
    async with async_session() as db:
        account = await _healthy_pixiv_account(db)
        if account is None:
            await db.rollback()
            return {"status": "skipped", "reason": "no_healthy_account"}

        credentials = RemoteAccountService(db, account.user_id).credentials_for_adapter(account)
        await db.commit()
        adapter = registry.get("pixiv")
        if not isinstance(adapter, PixivRemoteDiscoveryAdapter):
            raise TypeError("Registered Pixiv adapter cannot fetch rankings")

        results = []
        for mode in PIXIV_RANKING_MODES:
            result = await adapter.fetch_rankings(
                credentials,
                mode=mode,
                ranking_date=ranking_date,
            )
            if not result.items:
                raise PixivRankingDateNotReady(
                    f"Pixiv ranking {mode} is not ready for {ranking_date.isoformat()}"
                )
            results.append(result)

        outcome = await store_pixiv_ranking_results(db, results)
        await db.commit()
        return {
            "status": "completed",
            "ranking_date": ranking_date.isoformat(),
            **outcome,
        }


def sync_pixiv_rankings(ranking_date_iso: str | None = None):
    try:
        return asyncio.run(sync_pixiv_rankings_async(ranking_date_iso))
    except PixivRankingDateNotReady:
        from rq import Retry

        return Retry(max=2, interval=[30 * 60, 120 * 60])


__all__ = ["sync_pixiv_rankings", "sync_pixiv_rankings_async"]
