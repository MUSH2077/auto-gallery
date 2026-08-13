"""Deterministic activation of one executable source for automatic subscriptions."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source_creator import SourceCreator
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.providers import registry


PROVIDER_PRIORITY = {
    "pixiv": 0,
    "x": 1,
    "weibo": 2,
    "lofter": 3,
    "danbooru": 4,
}


@dataclass(frozen=True, slots=True)
class SourceSelection:
    source: SubscriptionSource
    reason: str
    normalized_url: str


def configured_auto_enable_sources() -> tuple[str, ...]:
    """Return the stable provider order configured for import auto-activation."""

    config_path = Path(
        os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config")
    ) / "config.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        extractors = payload.get("extractor", {})
        configured: list[str] = []
        for raw_name, config in extractors.items():
            if not isinstance(config, dict) or config.get("auto-enable-on-import") is not True:
                continue
            name = "x" if raw_name == "twitter" else str(raw_name)
            if name not in configured:
                configured.append(name)
        if configured:
            return tuple(configured)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    return ("pixiv",)


def eligible_source(source: SubscriptionSource) -> tuple[bool, str | None]:
    """Validate that a source is executable without requiring healthy auth."""

    if not source.source_url:
        return False, None
    try:
        provider = registry.get(source.source)
    except KeyError:
        return False, None
    if not provider.capabilities.can_download:
        return False, None
    normalized = provider.normalize_url(source.source_url) or source.source_url
    if not provider.validate_url(normalized):
        return False, None
    return True, normalized


async def select_primary_subscription_source(
    db: AsyncSession,
    subscription: Subscription,
    *,
    sources: list[SubscriptionSource] | None = None,
) -> SourceSelection | None:
    """Choose one source with a stable, explainable ordering."""

    if sources is None:
        sources = list((await db.execute(
            select(SubscriptionSource)
            .where(SubscriptionSource.subscription_id == subscription.id)
            .order_by(SubscriptionSource.id)
        )).scalars())

    disk_rows = list((await db.execute(
        select(
            SourceCreator.source,
            SourceCreator.source_creator_id,
            SourceCreator.raw_metadata,
        )
        .where(
            SourceCreator.creator_id == subscription.creator_id,
            SourceCreator.raw_metadata.is_not(None),
        )
    )).all())
    disk_identities = {
        (row.source, str(row.source_creator_id or ""))
        for row in disk_rows
        if isinstance(row.raw_metadata, dict)
        and isinstance(row.raw_metadata.get("_disk_import"), dict)
    }
    configured = configured_auto_enable_sources()
    configured_rank = {name: index for index, name in enumerate(configured)}
    candidates: list[tuple[tuple, SourceSelection]] = []
    for source in sources:
        eligible, normalized = eligible_source(source)
        if not eligible or normalized is None:
            continue
        identity = (source.source, str(source.source_creator_id or ""))
        is_disk = identity in disk_identities
        if source.is_enabled:
            category = 0
            reason = "already_enabled"
            category_rank = 0
        elif source.source in configured_rank:
            category = 1
            reason = "configured_auto_enable"
            category_rank = configured_rank[source.source]
        elif is_disk:
            category = 2
            reason = "disk_import_origin"
            category_rank = PROVIDER_PRIORITY.get(source.source, 100)
        else:
            category = 3
            reason = "provider_fallback"
            category_rank = PROVIDER_PRIORITY.get(source.source, 100)
        candidates.append((
            (
                category,
                category_rank,
                0 if source.auth_healthy else 1,
                PROVIDER_PRIORITY.get(source.source, 100),
                source.created_at.isoformat() if source.created_at else "",
                str(source.id),
            ),
            SourceSelection(source=source, reason=reason, normalized_url=normalized),
        ))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


async def lock_subscription_sources(
    db: AsyncSession,
    subscription_id: UUID,
) -> list[SubscriptionSource]:
    """Acquire source locks in UUID order to keep activation deadlock-safe."""

    return list((await db.execute(
        select(SubscriptionSource)
        .where(SubscriptionSource.subscription_id == subscription_id)
        .order_by(SubscriptionSource.id)
        .with_for_update()
    )).scalars())
