"""Build canonical index documents without depending on query execution.

Incremental delivery and rebuild consume the same index description and build
entrypoint. SearchService inherits the old private methods for compatibility.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable, Awaitable, Iterable
from uuid import UUID
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import async_session
from app.models import (
    Asset, AssetSource, Creator, CreatorAlias, DownloadJob, SourceCreator,
    Subscription, SubscriptionSource, Tag, TaskRun, UserSubscription,
    UserSubscriptionSource, Work, WorkCurationState, WorkSource, WorkSourceTag, WorkTag,
    ImportCurationOutbox,
)
from app.services.search_index_definitions import (
    WORKS_INDEX, CREATORS_INDEX, TAGS_INDEX, REPOSITORIES_INDEX, SUBSCRIPTIONS_INDEX,
    MEMBERSHIPS_INDEX, WORK_PROJECTION_VERSION, WORK_DOCUMENT_BATCH_SIZE,
    MEILI_DOCUMENT_PAYLOAD_BYTES,
)
from app.services.search_projection_fields import (
    _alias_projection_fields, _iso, _merge_alias_projection_fields,
    _normalize_reference_name, _thumbnail_projection, _timestamp, _with_projection_hash,
)
from app.services.search_pagination import _meili_shuffle_key, _meili_shuffle_parts

def _normalize_url(value: str | None) -> str:
    return (value or "").strip().rstrip("/").lower()


def _document_batches(
    documents: Iterable[dict[str, Any]],
    *,
    max_documents: int = WORK_DOCUMENT_BATCH_SIZE,
    max_bytes: int = MEILI_DOCUMENT_PAYLOAD_BYTES,
) -> Iterable[list[dict[str, Any]]]:
    """Yield JSON batches bounded by both document count and wire bytes."""

    batch: list[dict[str, Any]] = []
    # JSON array brackets; each additional element also needs one comma.
    batch_bytes = 2
    for document in documents:
        document_bytes = len(json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8"))
        if document_bytes + 2 > max_bytes:
            raise ValueError(
                f"Search document {document.get('id')} exceeds {max_bytes} bytes"
            )
        separator_bytes = 1 if batch else 0
        if batch and (
            len(batch) >= max_documents
            or batch_bytes + separator_bytes + document_bytes > max_bytes
        ):
            yield batch
            batch = []
            batch_bytes = 2
            separator_bytes = 0
        batch.append(document)
        batch_bytes += separator_bytes + document_bytes
    if batch:
        yield batch


class SearchProjection:
    def __init__(self, db: AsyncSession, *, parallel_hydration: bool = False):
        self.db = db
        self._parallel_hydration = parallel_hydration
        self._repository_maps: tuple[
            dict[tuple[str, str], list[str]],
            dict[str, list[str]],
        ] | None = None

    async def build(self, index_uid: str, entity_ids: Iterable[UUID]) -> list[dict]:
        """Build a bounded identity set using the index's canonical mapping."""
        try:
            spec = PROJECTION_SPECS[index_uid]
        except KeyError:
            raise ValueError(f"Unsupported search projection index {index_uid}") from None
        return await spec.build(self, entity_ids)

    async def _repository_lookup(self) -> tuple[dict[tuple[str, str], list[str]], dict[str, list[str]]]:
        if self._repository_maps is not None:
            return self._repository_maps
        rows = (await self.db.execute(select(
            SubscriptionSource.id,
            SubscriptionSource.source,
            SubscriptionSource.source_creator_id,
            SubscriptionSource.source_url,
        ))).all()
        by_identity: dict[tuple[str, str], list[str]] = defaultdict(list)
        by_url: dict[str, list[str]] = defaultdict(list)
        for repo_id, source, source_creator_id, source_url in rows:
            if source_creator_id:
                by_identity[(source, source_creator_id)].append(str(repo_id))
            if source_url:
                by_url[_normalize_url(source_url)].append(str(repo_id))
        self._repository_maps = (by_identity, by_url)
        return self._repository_maps

    async def _creator_alias_projections(
        self,
        creator_ids: Iterable[UUID],
    ) -> dict[str, dict[str, list[Any]]]:
        identities = tuple(dict.fromkeys(creator_ids))
        if not identities:
            return {}
        rows = list(
            (
                await self.db.execute(
                    select(CreatorAlias)
                    .where(CreatorAlias.creator_id.in_(identities))
                    .order_by(
                        CreatorAlias.creator_id,
                        CreatorAlias.is_current.desc(),
                        CreatorAlias.kind,
                        CreatorAlias.normalized_value,
                    )
                )
            ).scalars()
        )
        grouped: dict[str, list[CreatorAlias]] = defaultdict(list)
        for row in rows:
            grouped[str(row.creator_id)].append(row)
        projections: dict[str, dict[str, list[Any]]] = {}
        for creator_id, aliases in grouped.items():
            projection = _alias_projection_fields(aliases)
            projection["alias_records"] = [
                {**record, "creator_id": creator_id}
                for record in projection["alias_records"]
            ]
            projections[creator_id] = projection
        return projections

    async def _build_work_documents(self, work_ids: Iterable[UUID] | None = None) -> list[dict]:
        if work_ids is None:
            raise ValueError(
                "Full work projections must use the keyset streaming rebuild; "
                "an unbounded in-memory document list is forbidden"
            )

        stmt = select(Work, WorkCurationState.visibility).outerjoin(
            WorkCurationState, WorkCurationState.work_id == Work.id,
        ).where(~exists().where(and_(
            ImportCurationOutbox.work_id == Work.id,
            ImportCurationOutbox.state != "complete",
        )))
        if work_ids is not None:
            ids = list(work_ids)
            if not ids:
                return []
            stmt = stmt.where(Work.id.in_(ids))
        work_rows = (await self.db.execute(stmt.order_by(Work.created_at.desc()))).all()
        if not work_rows:
            return []
        ids = [work.id for work, _visibility in work_rows]

        if self._parallel_hydration:
            # The four hydration scans below intentionally use independent
            # committed sessions. Release the base Work snapshot before those
            # potentially long NAS scans begin; otherwise PostgreSQL's
            # idle_in_transaction_session_timeout can close this connection
            # while it waits for gather(), causing an otherwise successful
            # batch to fail during session rollback. expire_on_commit=False
            # keeps the already-loaded Work rows available for serialization.
            await self.db.commit()

        tag_statement = (
            select(WorkTag.work_id, Tag.normalized_name)
            .join(Tag, Tag.id == WorkTag.tag_id)
            .where(WorkTag.work_id.in_(ids))
        )
        source_tag_statement = (
            select(WorkSource.work_id, Tag.normalized_name)
            .join(WorkSourceTag, WorkSourceTag.work_source_id == WorkSource.id)
            .join(Tag, Tag.id == WorkSourceTag.tag_id)
            .where(WorkSource.work_id.in_(ids))
        )
        source_statement = (
            select(
                WorkSource.id,
                WorkSource.work_id,
                WorkSource.source,
                WorkSource.source_work_id,
                WorkSource.source_creator_id,
                WorkSource.source_url,
                SourceCreator.creator_id,
                SourceCreator.display_name,
                Creator.name,
                Creator.display_name,
            )
            .outerjoin(
                SourceCreator,
                and_(
                    SourceCreator.source == WorkSource.source,
                    SourceCreator.source_creator_id == WorkSource.source_creator_id,
                ),
            )
            .outerjoin(Creator, Creator.id == SourceCreator.creator_id)
            .where(WorkSource.work_id.in_(ids))
        )
        # Join through WorkSource so media hydration can start at the same time
        # as source hydration instead of waiting for a Python ID map first.
        asset_statement = (
            select(
                WorkSource.work_id,
                Asset.id,
                Asset.mime_type,
                Asset.file_name,
                Asset.width,
                Asset.height,
            )
            .select_from(WorkSource)
            .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
            .join(Asset, Asset.id == AssetSource.asset_id)
            .where(WorkSource.work_id.in_(ids))
            .order_by(WorkSource.work_id, AssetSource.ordinal, Asset.file_name)
        )

        if self._parallel_hydration:
            async def _committed_rows(statement: Any) -> list[Any]:
                async with async_session() as hydration_db:
                    return list((await hydration_db.execute(statement)).all())

            tag_rows, source_tag_rows, source_rows, asset_rows = await asyncio.gather(
                _committed_rows(tag_statement),
                _committed_rows(source_tag_statement),
                _committed_rows(source_statement),
                _committed_rows(asset_statement),
            )
        else:
            tag_rows = (await self.db.execute(tag_statement)).all()
            source_tag_rows = (await self.db.execute(source_tag_statement)).all()
            source_rows = (await self.db.execute(source_statement)).all()
            asset_rows = (await self.db.execute(asset_statement)).all()

        tags: dict[str, set[str]] = defaultdict(set)
        for work_id, name in tag_rows:
            tags[str(work_id)].add(name)
        for work_id, name in source_tag_rows:
            tags[str(work_id)].add(name)

        sources: dict[str, set[str]] = defaultdict(set)
        source_work_ids: dict[str, set[str]] = defaultdict(set)
        source_creator_keys: dict[str, set[str]] = defaultdict(set)
        source_work_keys: dict[str, set[str]] = defaultdict(set)
        creator_ids: dict[str, set[str]] = defaultdict(set)
        creator_names: dict[str, set[str]] = defaultdict(set)
        repository_ids: dict[str, set[str]] = defaultdict(set)
        repository_by_identity, repository_by_url = await self._repository_lookup()
        for source_id, work_id, source, source_work_id, source_creator_id, source_url, creator_id, source_name, creator_name, creator_display in source_rows:
            key = str(work_id)
            sources[key].add(source)
            source_work_ids[key].add(source_work_id)
            source_work_keys[key].add(f"{source}/{source_work_id}")
            if source_creator_id:
                source_creator_keys[key].add(f"{source}/{source_creator_id}")
            if creator_id:
                creator_ids[key].add(str(creator_id))
            display = creator_display or source_name or creator_name
            if display:
                creator_names[key].add(display)
            if source_creator_id:
                repository_ids[key].update(repository_by_identity.get((source, source_creator_id), []))
            if source_url:
                repository_ids[key].update(repository_by_url.get(_normalize_url(source_url), []))

        all_creator_ids = {
            UUID(creator_id)
            for values in creator_ids.values()
            for creator_id in values
        }
        alias_by_creator = await self._creator_alias_projections(all_creator_ids)

        asset_ids: dict[str, list[str]] = defaultdict(list)
        asset_id_sets: dict[str, set[str]] = defaultdict(set)
        asset_mimes: dict[str, set[str]] = defaultdict(set)
        asset_names: dict[str, set[str]] = defaultdict(set)
        asset_dimensions: dict[str, dict[str, tuple[int | None, int | None]]] = defaultdict(dict)
        for work_id, asset_id, mime, file_name, width, height in asset_rows:
            work_key = str(work_id)
            rendered_asset_id = str(asset_id)
            if rendered_asset_id not in asset_id_sets[work_key]:
                asset_id_sets[work_key].add(rendered_asset_id)
                asset_ids[work_key].append(rendered_asset_id)
            if mime:
                asset_mimes[work_key].add(mime.lower())
            asset_names[work_key].add((file_name or "").lower())
            asset_dimensions[work_key][rendered_asset_id] = (width, height)

        documents = []
        for work, visibility in work_rows:
            key = str(work.id)
            mimes = asset_mimes[key]
            names = asset_names[key]
            has_video = (
                any(mime.startswith("video/") for mime in mimes)
                or any(PurePosixPath(name).suffix in {".mp4", ".webm"} for name in names)
            )
            has_animation = (
                any(mime in {"image/gif", "image/apng"} for mime in mimes)
                or any(PurePosixPath(name).suffix in {".gif", ".zip"} for name in names)
            )
            has_image = any(mime.startswith("image/") for mime in mimes)
            previews = asset_ids[key][:10]
            thumbnail_asset_id, thumbnail_width, thumbnail_height = _thumbnail_projection(
                work,
                previews,
                asset_dimensions[key],
            )
            ordered_creator_names = sorted(creator_names[key])
            ordered_creator_ids = sorted(creator_ids[key])
            ordered_sources = sorted(sources[key])
            aliases = _merge_alias_projection_fields(
                alias_by_creator.get(creator_id, _alias_projection_fields(()))
                for creator_id in ordered_creator_ids
            )
            documents.append(_with_projection_hash({
                "id": key,
                "title": work.title or "",
                "description": (work.description or "")[:1000],
                "creator_name": ordered_creator_names[0] if ordered_creator_names else "",
                "creator_names": ordered_creator_names,
                "creator_id": ordered_creator_ids[0] if ordered_creator_ids else "",
                "creator_ids": ordered_creator_ids,
                **aliases,
                "repository_ids": sorted(repository_ids[key]),
                "source": ordered_sources[0] if ordered_sources else "unknown",
                "sources": ordered_sources,
                "source_work_ids": sorted(source_work_ids[key]),
                "source_creator_keys": sorted(source_creator_keys[key]),
                "source_work_keys": sorted(source_work_keys[key]),
                "tags": sorted(tags[key]),
                "is_nsfw": bool(work.is_nsfw),
                "is_ai_generated": bool(work.is_ai_generated),
                "is_favorite": bool(work.is_favorite),
                "heat_available": work.heat_score is not None,
                "heat_score": float(work.heat_score) if work.heat_score is not None else None,
                "shuffle_key": _meili_shuffle_key(work.shuffle_key),
                **_meili_shuffle_parts(work.shuffle_key, work.id),
                "visibility": visibility or "visible",
                "curation_visibility": visibility or "visible",
                "thumbnail_asset_id": thumbnail_asset_id,
                "thumbnail_width": thumbnail_width,
                "thumbnail_height": thumbnail_height,
                "preview_asset_ids": previews,
                "asset_count": len(asset_ids[key]),
                "has_tags": bool(tags[key]),
                "has_description": bool((work.description or "").strip()),
                "has_multiple_assets": len(asset_ids[key]) > 1,
                "has_image": has_image,
                "has_animation": has_animation,
                "has_ugoira": has_animation,
                "has_video": has_video,
                "posted_at": _iso(work.posted_at),
                "created_at": _iso(work.created_at),
                "updated_at": _iso(work.updated_at),
                "posted_ts": _timestamp(work.posted_at),
                "created_ts": _timestamp(work.created_at),
                "updated_ts": _timestamp(work.updated_at),
            }, version=WORK_PROJECTION_VERSION))
        return documents

    async def _build_creator_documents(
        self,
        creator_ids: Iterable[UUID] | None = None,
    ) -> list[dict]:
        identities = tuple(creator_ids) if creator_ids is not None else None
        if identities is not None and not identities:
            return []
        creator_statement = select(Creator)
        if identities is not None:
            creator_statement = creator_statement.where(Creator.id.in_(identities))
        rows = (await self.db.execute(
            creator_statement.order_by(Creator.created_at.desc())
        )).scalars().all()
        selected_ids = [creator.id for creator in rows]
        alias_by_creator = await self._creator_alias_projections(selected_ids)
        source_rows = (await self.db.execute(
            select(SourceCreator.creator_id, SourceCreator.source, SourceCreator.source_creator_id)
            .where(
                SourceCreator.creator_id.isnot(None),
                SourceCreator.creator_id.in_(selected_ids),
            )
        )).all()
        sources: dict[str, set[str]] = defaultdict(set)
        source_ids: dict[str, set[str]] = defaultdict(set)
        source_creator_keys: dict[str, set[str]] = defaultdict(set)
        for creator_id, source, source_creator_id in source_rows:
            sources[str(creator_id)].add(source)
            source_ids[str(creator_id)].add(source_creator_id)
            source_creator_keys[str(creator_id)].add(f"{source}/{source_creator_id}")
        subscription_source_rows = (await self.db.execute(
            select(
                Subscription.creator_id,
                SubscriptionSource.source,
                SubscriptionSource.source_creator_id,
            )
            .join(
                SubscriptionSource,
                SubscriptionSource.subscription_id == Subscription.id,
            )
            .where(
                Subscription.creator_id.in_(selected_ids),
                SubscriptionSource.source_creator_id.is_not(None),
            )
        )).all()
        for creator_id, source, source_creator_id in subscription_source_rows:
            sources[str(creator_id)].add(source)
            source_ids[str(creator_id)].add(source_creator_id)
            source_creator_keys[str(creator_id)].add(f"{source}/{source_creator_id}")
        sub_rows = (await self.db.execute(
            select(
                Subscription.creator_id,
                func.count(func.distinct(Subscription.id)),
                func.count(SubscriptionSource.id),
                func.max(SubscriptionSource.last_synced_at),
            )
            .outerjoin(SubscriptionSource, SubscriptionSource.subscription_id == Subscription.id)
            .where(Subscription.creator_id.in_(selected_ids))
            .group_by(Subscription.creator_id)
        )).all()
        sub_counts = {
            str(creator_id): (int(sub_count), int(repo_count), last_synced)
            for creator_id, sub_count, repo_count, last_synced in sub_rows
        }
        return [{
            "id": str(creator.id),
            "name": creator.name,
            "name_sort": _normalize_reference_name(
                creator.display_name or creator.name
            ),
            "display_name": creator.display_name or creator.name,
            "description": (creator.description or "")[:1000],
            "thumbnail_url": creator.thumbnail_url,
            "is_active": bool(creator.is_active),
            "is_favorite": bool(creator.is_favorite),
            "danbooru_artist_id": creator.danbooru_artist_id,
            "has_danbooru": creator.danbooru_artist_id is not None,
            "has_subscription": sub_counts.get(str(creator.id), (0, 0, None))[0] > 0,
            "has_repository": sub_counts.get(str(creator.id), (0, 0, None))[1] > 0,
            "subscription_count": sub_counts.get(str(creator.id), (0, 0, None))[0],
            "repository_count": sub_counts.get(str(creator.id), (0, 0, None))[1],
            "source_count": len(sources[str(creator.id)]),
            "last_synced_at": _iso(sub_counts.get(str(creator.id), (0, 0, None))[2]),
            "sources": sorted(sources[str(creator.id)]),
            "source_creator_ids": sorted(source_ids[str(creator.id)]),
            "source_creator_keys": sorted(source_creator_keys[str(creator.id)]),
            **alias_by_creator.get(str(creator.id), _alias_projection_fields(())),
            "created_at": _iso(creator.created_at),
            "updated_at": _iso(creator.updated_at),
            "created_ts": _timestamp(creator.created_at),
            "updated_ts": _timestamp(creator.updated_at),
        } for creator in rows]

    async def _build_tag_documents(
        self,
        tag_ids: Iterable[UUID] | None = None,
    ) -> list[dict]:
        identities = tuple(tag_ids) if tag_ids is not None else None
        if identities is not None and not identities:
            return []
        direct_pairs = select(
            WorkTag.tag_id.label("tag_id"),
            WorkTag.work_id.label("work_id"),
        )
        sourced_pairs = select(
            WorkSourceTag.tag_id.label("tag_id"),
            WorkSource.work_id.label("work_id"),
        ).join(
            WorkSource,
            WorkSource.id == WorkSourceTag.work_source_id,
        )
        if identities is not None:
            direct_pairs = direct_pairs.where(WorkTag.tag_id.in_(identities))
            sourced_pairs = sourced_pairs.where(WorkSourceTag.tag_id.in_(identities))
        tag_work_pairs = (
            direct_pairs
            .union_all(sourced_pairs)
            .subquery()
        )
        usage = (
            select(
                tag_work_pairs.c.tag_id,
                func.count(func.distinct(tag_work_pairs.c.work_id)).label("usage_count"),
            )
            .group_by(tag_work_pairs.c.tag_id)
            .subquery()
        )
        tag_statement = select(Tag, func.coalesce(usage.c.usage_count, 0)).outerjoin(
            usage,
            usage.c.tag_id == Tag.id,
        )
        if identities is not None:
            tag_statement = tag_statement.where(Tag.id.in_(identities))
        rows = (await self.db.execute(
            tag_statement
            .order_by(Tag.created_at.desc())
        )).all()
        return [{
            "id": str(tag.id),
            "normalized_name": tag.normalized_name,
            "category": tag.category or "general",
            "usage_count": int(count or 0),
            "created_at": _iso(tag.created_at),
            "updated_at": _iso(tag.updated_at),
            "created_ts": _timestamp(tag.created_at),
            "updated_ts": _timestamp(tag.updated_at),
        } for tag, count in rows]

    async def _build_repository_documents(
        self,
        repository_ids: Iterable[UUID] | None = None,
    ) -> list[dict]:
        identities = tuple(repository_ids) if repository_ids is not None else None
        if identities is not None and not identities:
            return []
        statement = (
            select(SubscriptionSource, Subscription, Creator)
            .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
            .join(Creator, Creator.id == Subscription.creator_id)
        )
        if identities is not None:
            statement = statement.where(SubscriptionSource.id.in_(identities))
        rows = (await self.db.execute(
            statement.order_by(SubscriptionSource.created_at.desc())
        )).all()
        alias_by_creator = await self._creator_alias_projections(
            creator.id for _repo, _subscription, creator in rows
        )
        from app.services.auth_health import classify_source_health

        def _health_fields(repo: SubscriptionSource, subscription: Subscription) -> dict:
            health = classify_source_health(repo, subscription)
            return {
                "auth_healthy": repo.auth_healthy is not False,
                "auth_state": health.auth_state,
                "credential_state": health.credential_state,
            }

        return [{
            "id": str(repo.id),
            "name": f"{repo.source}/{repo.source_creator_id}" if repo.source_creator_id else (repo.source_url or str(repo.id)),
            "name_sort": (repo.source_creator_id or repo.source_url or str(repo.id)).lower(),
            "source": repo.source,
            "source_creator_id": repo.source_creator_id,
            "source_creator_keys": (
                [f"{repo.source}/{repo.source_creator_id}"]
                if repo.source_creator_id else []
            ),
            "source_url": repo.source_url,
            "creator_id": str(creator.id),
            "creator_name": creator.display_name or creator.name,
            **alias_by_creator.get(str(creator.id), _alias_projection_fields(())),
            "subscription_id": str(subscription.id),
            "subscription_name": subscription.name,
            "is_enabled": bool(repo.is_enabled),
            **_health_fields(repo, subscription),
            "auth_status": repo.auth_status,
            "has_last_sync": repo.last_synced_at is not None,
            "has_source_creator_id": bool(repo.source_creator_id),
            "last_synced_at": _iso(repo.last_synced_at),
            "created_at": _iso(repo.created_at),
            "updated_at": _iso(repo.updated_at),
            "created_ts": _timestamp(repo.created_at),
            "updated_ts": _timestamp(repo.updated_at),
            "synced_ts": _timestamp(repo.last_synced_at),
        } for repo, subscription, creator in rows]

    async def _build_membership_documents(self, membership_ids: Iterable[UUID]) -> list[dict]:
        """Build one bounded identity window using the canonical search vocabulary."""
        identities = tuple(membership_ids)
        if not identities:
            return []
        if len(identities) > 500:
            raise ValueError("Membership projection batches must contain at most 500 IDs")
        members = list((await self.db.execute(
            select(UserSubscription).where(UserSubscription.id.in_(identities))
            .order_by(UserSubscription.id)
        )).scalars())
        canonical = {doc["id"]: doc for doc in await self._build_subscription_documents(
            tuple({member.subscription_id for member in members})
        )}
        stats = {row[0]: row[1:] for row in (await self.db.execute(
            select(UserSubscriptionSource.user_subscription_id,
                   func.max(UserSubscriptionSource.last_synced_at),
                   func.count(UserSubscriptionSource.id),
                   func.count(UserSubscriptionSource.id).filter(UserSubscriptionSource.is_enabled.is_(True)))
            .where(UserSubscriptionSource.user_subscription_id.in_(identities))
            .group_by(UserSubscriptionSource.user_subscription_id)
        )).all()}
        documents = []
        for member in members:
            source_doc = canonical.get(str(member.subscription_id))
            if source_doc is None:
                continue
            latest, count, enabled = stats.get(member.id, (None, 0, 0))
            document = {
                **source_doc, "id": str(member.id), "user_id": member.user_id,
                "subscription_id": str(member.subscription_id),
                "canonical_name": source_doc["name"], "name": member.name or "",
                "is_active": bool(member.is_active), "sync_enabled": bool(member.sync_enabled),
                "sync_interval_hours": member.sync_interval_hours,
                "schedule_mode": member.schedule_mode, "schedule_rule": member.schedule_rule,
                "scheduled_times": member.scheduled_times,
                "last_synced_at": _iso(latest), "synced_ts": _timestamp(latest),
                "never_synced": latest is None, "has_last_sync": latest is not None,
                "source_count": int(count), "enabled_source_count": int(enabled),
                "updated_at": _iso(member.updated_at), "updated_ts": _timestamp(member.updated_at),
            }
            documents.append(_with_projection_hash(document))
        return documents

    async def _build_subscription_documents(
        self,
        subscription_ids: Iterable[UUID] | None = None,
    ) -> list[dict]:
        identities = tuple(subscription_ids) if subscription_ids is not None else None
        if identities is not None and not identities:
            return []
        subscription_statement = (
            select(Subscription, Creator)
            .join(Creator, Creator.id == Subscription.creator_id)
        )
        if identities is not None:
            subscription_statement = subscription_statement.where(
                Subscription.id.in_(identities)
            )
        rows = (await self.db.execute(
            subscription_statement.order_by(Subscription.created_at.desc())
        )).all()
        alias_by_creator = await self._creator_alias_projections(
            creator.id for _subscription, creator in rows
        )
        selected_ids = [subscription.id for subscription, _creator in rows]
        source_rows = (await self.db.execute(
            select(SubscriptionSource)
            .where(SubscriptionSource.subscription_id.in_(selected_ids))
            .order_by(SubscriptionSource.created_at)
        )).scalars().all()
        by_subscription: dict[str, list[SubscriptionSource]] = defaultdict(list)
        for repository in source_rows:
            by_subscription[str(repository.subscription_id)].append(repository)

        running_statuses = {"enqueued", "downloading", "downloaded", "importing"}
        index_safe_job = and_(
            DownloadJob.owner_user_id.is_(None),
            DownloadJob.triggering_user_subscription_id.is_(None),
            DownloadJob.triggering_remote_account_id.is_(None),
        )
        index_safe_task = and_(
            TaskRun.owner_user_id.is_(None),
            TaskRun.triggering_user_subscription_id.is_(None),
            TaskRun.triggering_remote_account_id.is_(None),
        )
        job_stats_rows = (await self.db.execute(
            select(
                DownloadJob.subscription_id,
                func.count(DownloadJob.id).filter(
                    DownloadJob.status.in_(running_statuses)
                ),
            )
            .where(
                DownloadJob.subscription_id.in_(selected_ids),
                index_safe_job,
            )
            .group_by(DownloadJob.subscription_id)
        )).all()
        running_stats = {
            str(subscription_id): int(running)
            for subscription_id, running in job_stats_rows
        }
        attention_rows = (await self.db.execute(
            select(
                DownloadJob.subscription_id,
                func.count(TaskRun.id.distinct()),
            )
            .join(
                TaskRun,
                and_(
                    TaskRun.subject_type == "download_job",
                    TaskRun.subject_id == DownloadJob.id,
                ),
            )
            .where(
                DownloadJob.subscription_id.in_(selected_ids),
                TaskRun.attention_state == "open",
                index_safe_job,
                index_safe_task,
            )
            .group_by(DownloadJob.subscription_id)
        )).all()
        attention_stats = {
            str(subscription_id): int(attention)
            for subscription_id, attention in attention_rows
        }
        # Compatibility fields now describe only actionable operational rows;
        # completed and resolved history belongs to repository receipts.
        latest_job_rows = (await self.db.execute(
            select(
                DownloadJob.subscription_id,
                DownloadJob.id,
                TaskRun.status,
                TaskRun.updated_at,
            )
            .join(
                TaskRun,
                and_(
                    TaskRun.subject_type == "download_job",
                    TaskRun.subject_id == DownloadJob.id,
                ),
            )
            .where(
                DownloadJob.subscription_id.in_(selected_ids),
                index_safe_job,
                index_safe_task,
            )
            .where(or_(
                TaskRun.status.in_({"enqueued", "running", "paused", "recovering"}),
                TaskRun.attention_state == "open",
            ))
            .distinct(DownloadJob.subscription_id)
            .order_by(
                DownloadJob.subscription_id,
                TaskRun.updated_at.desc(),
                TaskRun.id.desc(),
            )
        )).all()
        latest_jobs = {
            str(subscription_id): (job_id, status, created_at)
            for subscription_id, job_id, status, created_at in latest_job_rows
        }
        documents = []
        for subscription, creator in rows:
            repositories = by_subscription[str(subscription.id)]
            running_jobs = running_stats.get(str(subscription.id), 0)
            failed_jobs = attention_stats.get(str(subscription.id), 0)
            latest_job = latest_jobs.get(str(subscription.id))
            latest_sync = max((repo.last_synced_at for repo in repositories if repo.last_synced_at), default=subscription.last_synced_at)
            documents.append({
                "id": str(subscription.id),
                "name": subscription.name or creator.display_name or creator.name,
                "name_sort": _normalize_reference_name(
                    creator.display_name or creator.name
                ),
                "creator_id": str(creator.id),
                "creator_name": creator.display_name or creator.name,
                **alias_by_creator.get(str(creator.id), _alias_projection_fields(())),
                "is_active": bool(subscription.is_active),
                "sync_enabled": bool(subscription.sync_enabled),
                "sync_interval_hours": subscription.sync_interval_hours,
                "schedule_mode": subscription.schedule_mode,
                "scheduled_times": subscription.scheduled_times,
                "schedule_rule": subscription.schedule_rule,
                "never_synced": latest_sync is None,
                "has_last_sync": latest_sync is not None,
                "last_synced_at": _iso(latest_sync),
                "repository_ids": [str(repo.id) for repo in repositories],
                "source_count": len(repositories),
                "enabled_source_count": sum(1 for repo in repositories if repo.is_enabled),
                "running_job_count": running_jobs,
                "failed_job_count": failed_jobs,
                "latest_job_id": str(latest_job[0]) if latest_job else None,
                "latest_job_status": latest_job[1] if latest_job else None,
                "latest_job_created_at": _iso(latest_job[2]) if latest_job else None,
                "sources": sorted({repo.source for repo in repositories}),
                "source_urls": [repo.source_url for repo in repositories if repo.source_url],
                "source_creator_ids": [repo.source_creator_id for repo in repositories if repo.source_creator_id],
                "source_creator_keys": [
                    f"{repo.source}/{repo.source_creator_id}"
                    for repo in repositories if repo.source_creator_id
                ],
                "created_at": _iso(subscription.created_at),
                "updated_at": _iso(subscription.updated_at),
                "created_ts": _timestamp(subscription.created_at),
                "updated_ts": _timestamp(subscription.updated_at),
                "synced_ts": _timestamp(latest_sync),
            })
        return documents


@dataclass(frozen=True)
class ProjectionSpec:
    model: Any
    build: Callable[[SearchProjection, Iterable[UUID]], Awaitable[list[dict]]]


PROJECTION_SPECS = {
    WORKS_INDEX: ProjectionSpec(Work, SearchProjection._build_work_documents),
    CREATORS_INDEX: ProjectionSpec(Creator, SearchProjection._build_creator_documents),
    TAGS_INDEX: ProjectionSpec(Tag, SearchProjection._build_tag_documents),
    REPOSITORIES_INDEX: ProjectionSpec(SubscriptionSource, SearchProjection._build_repository_documents),
    SUBSCRIPTIONS_INDEX: ProjectionSpec(Subscription, SearchProjection._build_subscription_documents),
    MEMBERSHIPS_INDEX: ProjectionSpec(UserSubscription, SearchProjection._build_membership_documents),
}
