"""Resolve repository-scoped source creator identities for legacy data."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source_creator import SourceCreator
from app.models.subscription_source import SubscriptionSource
from app.providers import registry


def _normalized_url(source: str, value: str | None) -> str:
    if not value:
        return ""
    try:
        provider = registry.get(source)
        return (provider.normalize_url(value) or value).rstrip("/").casefold()
    except Exception:
        return value.rstrip("/").casefold()


async def resolve_repository_source_creator_ids(
    db: AsyncSession,
    subscription_source: SubscriptionSource,
    creator_id: UUID,
) -> list[str]:
    """Return source creator IDs belonging to one repository.

    New imports persist ``SubscriptionSource.source_creator_id`` directly.
    Older rows may be empty, so we fall back to source creator records owned by
    the subscription's creator and prefer an exact normalized profile URL.
    """
    if subscription_source.source_creator_id:
        return [subscription_source.source_creator_id]

    result = await db.execute(
        select(SourceCreator)
        .where(
            SourceCreator.creator_id == creator_id,
            SourceCreator.source == subscription_source.source,
        )
        .order_by(SourceCreator.created_at, SourceCreator.id)
    )
    candidates = list(result.scalars().all())
    if not candidates:
        return []

    repository_url = _normalized_url(
        subscription_source.source,
        subscription_source.source_url,
    )
    if repository_url:
        exact = [
            row.source_creator_id
            for row in candidates
            if _normalized_url(row.source, row.source_url) == repository_url
        ]
        if exact:
            return list(dict.fromkeys(exact))

    if len(candidates) == 1:
        return [candidates[0].source_creator_id]

    try:
        provider = registry.get(subscription_source.source)
        repository_dir = provider.get_creator_dir_from_url(
            subscription_source.source_url or "",
        )
    except Exception:
        repository_dir = None
    if repository_dir:
        matches = []
        for candidate in candidates:
            try:
                candidate_dir = provider.get_creator_dir_from_url(candidate.source_url or "")
            except Exception:
                candidate_dir = None
            if candidate_dir and str(candidate_dir).casefold() == str(repository_dir).casefold():
                matches.append(candidate.source_creator_id)
        if matches:
            return list(dict.fromkeys(matches))

    return []
