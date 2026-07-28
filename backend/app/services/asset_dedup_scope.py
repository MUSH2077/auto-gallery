"""Authoritative scope rules for cross-source asset reconciliation.

Visual reconciliation exists only to identify the same image across canonical
sources.  Assets from the same Work are intentional pages/variations and
assets from the same source are outside this module's domain, regardless of
their hashes or visual similarity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AssetSource, WorkSource
from app.services.settings import source_key_for_extractor


def canonical_source(value: str) -> str:
    """Return the domain source key used by WorkSource and public APIs."""

    return source_key_for_extractor(value.strip().lower())


@dataclass(frozen=True)
class AssetDedupContext:
    asset_id: UUID
    sources: frozenset[str]
    raw_sources: frozenset[str]
    work_ids: frozenset[UUID]
    work_source_ids: frozenset[UUID]

    @property
    def complete(self) -> bool:
        return bool(self.sources and self.work_ids and self.work_source_ids)

    def facts(self) -> dict[str, list[str]]:
        return {
            "sources": sorted(self.sources),
            "work_ids": sorted(str(value) for value in self.work_ids),
            "work_source_ids": sorted(
                str(value) for value in self.work_source_ids
            ),
        }


@dataclass(frozen=True)
class DedupScopeDecision:
    eligible: bool
    reason: str
    contexts: tuple[AssetDedupContext, ...]

    def facts(self) -> dict:
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "assets": {
                str(context.asset_id): context.facts()
                for context in self.contexts
            },
        }


class CrossSourceDedupScope:
    """Deep module enforcing the complete cross-source eligibility contract."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def contexts(
        self,
        asset_ids: Iterable[UUID],
    ) -> dict[UUID, AssetDedupContext]:
        ordered_ids = tuple(dict.fromkeys(asset_ids))
        if not ordered_ids:
            return {}
        rows = await self.db.execute(
            select(
                AssetSource.asset_id,
                WorkSource.id.label("work_source_id"),
                WorkSource.work_id,
                WorkSource.source,
            )
            .select_from(AssetSource)
            .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
            .where(AssetSource.asset_id.in_(ordered_ids))
        )
        collected: dict[UUID, dict[str, set]] = {
            asset_id: {
                "sources": set(),
                "raw_sources": set(),
                "work_ids": set(),
                "work_source_ids": set(),
            }
            for asset_id in ordered_ids
        }
        for row in rows:
            values = collected[row.asset_id]
            raw_source = row.source.strip().lower()
            values["raw_sources"].add(raw_source)
            values["sources"].add(canonical_source(raw_source))
            values["work_ids"].add(row.work_id)
            values["work_source_ids"].add(row.work_source_id)
        return {
            asset_id: AssetDedupContext(
                asset_id=asset_id,
                sources=frozenset(values["sources"]),
                raw_sources=frozenset(values["raw_sources"]),
                work_ids=frozenset(values["work_ids"]),
                work_source_ids=frozenset(values["work_source_ids"]),
            )
            for asset_id, values in collected.items()
        }

    async def pair(
        self,
        left_id: UUID,
        right_id: UUID,
    ) -> DedupScopeDecision:
        contexts = await self.contexts((left_id, right_id))
        left = contexts[left_id]
        right = contexts[right_id]
        pair = (left, right)
        if not left.complete or not right.complete:
            return DedupScopeDecision(False, "missing_context", pair)
        if left.work_ids & right.work_ids:
            return DedupScopeDecision(False, "same_work", pair)
        if left.sources & right.sources:
            return DedupScopeDecision(False, "same_source", pair)
        return DedupScopeDecision(True, "eligible", pair)

    async def group(
        self,
        asset_ids: Iterable[UUID],
    ) -> DedupScopeDecision:
        ordered_ids = tuple(dict.fromkeys(asset_ids))
        contexts_by_id = await self.contexts(ordered_ids)
        contexts = tuple(contexts_by_id[asset_id] for asset_id in ordered_ids)
        if len(contexts) < 2 or any(not context.complete for context in contexts):
            return DedupScopeDecision(False, "missing_context", contexts)

        source_owners: dict[str, set[UUID]] = {}
        work_owners: dict[UUID, set[UUID]] = {}
        for context in contexts:
            for source in context.sources:
                source_owners.setdefault(source, set()).add(context.asset_id)
            for work_id in context.work_ids:
                work_owners.setdefault(work_id, set()).add(context.asset_id)

        if any(len(owners) > 1 for owners in work_owners.values()):
            return DedupScopeDecision(False, "group_work_conflict", contexts)
        if any(len(owners) > 1 for owners in source_owners.values()):
            return DedupScopeDecision(False, "group_source_conflict", contexts)
        if len(source_owners) < 2:
            return DedupScopeDecision(False, "same_source", contexts)
        return DedupScopeDecision(True, "eligible", contexts)


def scope_error_message(reason: str) -> str:
    return {
        "missing_context": (
            "Both assets must have a bound Work and canonical source"
        ),
        "same_work": "Assets from the same Work never enter reconciliation",
        "same_source": "Asset reconciliation is restricted to different sources",
        "group_work_conflict": (
            "A visual group may contain at most one asset from each Work"
        ),
        "group_source_conflict": (
            "A visual group may contain at most one asset from each source"
        ),
    }.get(reason, "Assets are outside the cross-source reconciliation scope")
