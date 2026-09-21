"""Creator identity alias normalization and observation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from datetime import datetime
import inspect
import re
import unicodedata
from urllib.parse import unquote, urlparse
from uuid import UUID

from sqlalchemy import delete, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.creator_alias import CreatorAlias
from app.models.creator import Creator
from app.models.creator_link import CreatorLink
from app.models.source_creator import SourceCreator
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.services.source_search_identity import parse_source_url


_DANBOORU_ALIAS_NOTE = re.compile(
    r"\ADanbooru artist tag: (?P<name>\S+?)(?: \(aka: (?P<aliases>.+)\))?\Z"
)


def normalize_creator_alias(value: str, *, kind: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = " ".join(normalized.split())
    if kind == "account" and normalized.startswith("@"):
        normalized = normalized[1:].lstrip()
    return normalized


@dataclass(frozen=True)
class AliasObservation:
    source: str
    kind: str
    value: str
    source_ref: str | None = None
    is_current: bool = True

    def __post_init__(self) -> None:
        if not normalize_creator_alias(self.value, kind=self.kind):
            raise ValueError("Creator alias value must not be empty.")


def extract_danbooru_note_aliases(notes: str | None) -> tuple[str, ...]:
    match = _DANBOORU_ALIAS_NOTE.fullmatch((notes or "").strip())
    if match is None:
        return ()
    raw_aliases = match.group("aliases")
    if raw_aliases is None:
        return (match.group("name"),)
    aliases = tuple(
        value.strip()
        for value in raw_aliases.split(",")
    )
    if not aliases or any(not value for value in aliases):
        return ()
    return (match.group("name"), *aliases)


def collect_creator_alias_observations(
    creator: object,
    *,
    source_creators: Iterable[object] = (),
    links: Iterable[object] = (),
    subscription_sources: Iterable[object] = (),
) -> tuple[AliasObservation, ...]:
    collected: list[AliasObservation] = []
    seen: set[tuple[str, str, str]] = set()

    def add(
        source: str,
        kind: str,
        value: object,
        source_ref: str,
    ) -> None:
        if not isinstance(value, (str, int)):
            return
        text = str(value).strip()
        normalized = normalize_creator_alias(text, kind=kind)
        if not normalized:
            return
        key = (source, kind, normalized)
        if key in seen:
            return
        seen.add(key)
        collected.append(
            AliasObservation(
                source=source,
                kind=kind,
                value=text,
                source_ref=source_ref,
            )
        )

    def add_creator_url(
        source: str,
        value: object,
        source_ref: str,
    ) -> None:
        if not isinstance(value, str):
            return
        parsed = parse_source_url(value)
        expected_source = "x" if source == "twitter" else source
        if (
            parsed is None
            or parsed.kind != "creator"
            or parsed.source != expected_source
        ):
            return
        add(parsed.source, "url", parsed.normalized_url, f"{source_ref}:url")
        handle = _url_handle(parsed.normalized_url, parsed.source)
        if handle:
            add(parsed.source, "url_handle", handle, f"{source_ref}:url_handle")

    creator_id = str(getattr(creator, "id", ""))
    add("local", "name", getattr(creator, "name", None), f"creator:{creator_id}:name")
    add(
        "local",
        "name",
        getattr(creator, "display_name", None),
        f"creator:{creator_id}:display_name",
    )

    for item in source_creators:
        source = str(getattr(item, "source", "") or "").casefold()
        ref = f"source_creator:{getattr(item, 'id', '')}"
        add(source, "name", getattr(item, "display_name", None), f"{ref}:display_name")
        add(source, "source_id", getattr(item, "source_creator_id", None), f"{ref}:source_id")
        add_creator_url(source, getattr(item, "source_url", None), ref)
        metadata = getattr(item, "raw_metadata", None) or {}
        if isinstance(metadata, dict):
            for key in ("account", "username", "screen_name"):
                add(source, "account", metadata.get(key), f"{ref}:{key}")

    for item in subscription_sources:
        source = str(getattr(item, "source", "") or "").casefold()
        ref = f"subscription_source:{getattr(item, 'id', '')}"
        add(source, "source_id", getattr(item, "source_creator_id", None), f"{ref}:source_id")
        add_creator_url(source, getattr(item, "source_url", None), ref)

    for item in links:
        url = getattr(item, "url", None)
        parsed = parse_source_url(url) if isinstance(url, str) else None
        ref = f"creator_link:{getattr(item, 'id', '')}"
        if parsed and parsed.kind == "creator":
            add(parsed.source, "url", parsed.normalized_url, f"{ref}:url")
            handle = _url_handle(parsed.normalized_url, parsed.source)
            if handle:
                add(parsed.source, "url_handle", handle, f"{ref}:url_handle")

        danbooru_names = extract_danbooru_note_aliases(getattr(item, "notes", None))
        if danbooru_names:
            add("danbooru", "name", danbooru_names[0], f"{ref}:danbooru_name")
            for position, alias in enumerate(danbooru_names[1:]):
                add(
                    "danbooru",
                    "other_name",
                    alias,
                    f"{ref}:danbooru_other_name:{position}",
                )

    return tuple(collected)


def _url_handle(url: object, source: str) -> str | None:
    if not isinstance(url, str):
        return None
    path = [unquote(part) for part in urlparse(url).path.split("/") if part]
    if source == "pixiv" and len(path) >= 2 and path[0].casefold() == "stacc":
        return path[1]
    if source == "x" and path and path[0].casefold() not in {"home", "search", "i"}:
        return path[0].lstrip("@")
    if source == "iwara" and len(path) >= 2 and path[0].casefold() in {"profile", "users"}:
        return path[1]
    if source == "lofter":
        hostname = (urlparse(url).hostname or "").casefold()
        suffix = ".lofter.com"
        if hostname.endswith(suffix) and hostname != f"www{suffix}":
            return hostname.removesuffix(suffix)
    return None


async def observe_creator_aliases(
    db: AsyncSession,
    creator_id: UUID,
    observations: Iterable[AliasObservation],
    *,
    request_projection: bool = True,
) -> None:
    values = tuple(observations)
    if not values:
        return

    for observation in values:
        normalized = normalize_creator_alias(
            observation.value,
            kind=observation.kind,
        )
        if observation.is_current and observation.source_ref:
            await db.execute(
                update(CreatorAlias)
                .where(
                    CreatorAlias.creator_id == creator_id,
                    CreatorAlias.source == observation.source,
                    CreatorAlias.kind == observation.kind,
                    CreatorAlias.source_ref == observation.source_ref,
                    CreatorAlias.normalized_value != normalized,
                    CreatorAlias.is_current.is_(True),
                )
                .values(is_current=False, last_seen_at=func.now())
            )

        statement = pg_insert(CreatorAlias).values(
            creator_id=creator_id,
            value=observation.value.strip(),
            normalized_value=normalized,
            source=observation.source,
            kind=observation.kind,
            is_current=observation.is_current,
            source_ref=observation.source_ref,
        )
        await db.execute(
            statement.on_conflict_do_update(
                constraint="uq_creator_aliases_identity",
                set_={
                    "value": statement.excluded.value,
                    "is_current": statement.excluded.is_current,
                    "source_ref": statement.excluded.source_ref,
                    "last_seen_at": func.now(),
                    "updated_at": func.now(),
                },
            )
        )

    if request_projection:
        from app.services.creator import CreatorService

        await CreatorService(db)._request_creator_projection(creator_id)


async def list_creator_aliases(
    db: AsyncSession,
    creator_id: UUID,
    *,
    include_history: bool = True,
) -> list[CreatorAlias]:
    creator_exists = await db.scalar(
        select(Creator.id).where(Creator.id == creator_id)
    )
    if creator_exists is None:
        raise ValueError("Creator not found")

    statement = select(CreatorAlias).where(
        CreatorAlias.creator_id == creator_id
    )
    if not include_history:
        statement = statement.where(CreatorAlias.is_current.is_(True))
    statement = statement.order_by(
        CreatorAlias.is_current.desc(),
        CreatorAlias.source.asc(),
        CreatorAlias.kind.asc(),
        CreatorAlias.normalized_value.asc(),
        CreatorAlias.id.asc(),
    )
    return list((await db.execute(statement)).scalars())


async def backfill_creator_alias_batch(
    db: AsyncSession,
    creator_ids: Iterable[UUID],
    *,
    request_projection: bool = True,
) -> dict[str, int]:
    ids = tuple(dict.fromkeys(creator_ids))
    if not ids:
        return {
            "creators": 0,
            "observations": 0,
            "malformed_danbooru_notes": 0,
        }

    creators = list(
        (
            await db.execute(
                select(Creator)
                .where(Creator.id.in_(ids))
                .order_by(Creator.created_at, Creator.id)
            )
        ).scalars()
    )
    stored_urls = (
        await db.execute(
            select(
                CreatorAlias.id,
                CreatorAlias.source,
                CreatorAlias.value,
            ).where(
                CreatorAlias.creator_id.in_(ids),
                CreatorAlias.kind == "url",
            )
        )
    ).all()
    invalid_url_ids = []
    for alias_id, source, value in stored_urls:
        parsed = parse_source_url(value)
        expected_source = "x" if source == "twitter" else source
        if (
            parsed is None
            or parsed.kind != "creator"
            or parsed.source != expected_source
        ):
            invalid_url_ids.append(alias_id)
    if invalid_url_ids:
        await db.execute(
            delete(CreatorAlias).where(CreatorAlias.id.in_(invalid_url_ids))
        )
    source_rows = list(
        (
            await db.execute(
                select(SourceCreator).where(SourceCreator.creator_id.in_(ids))
            )
        ).scalars()
    )
    link_rows = list(
        (
            await db.execute(
                select(CreatorLink).where(CreatorLink.creator_id.in_(ids))
            )
        ).scalars()
    )
    repository_rows = list(
        (
            await db.execute(
                select(SubscriptionSource, Subscription.creator_id)
                .join(
                    Subscription,
                    Subscription.id == SubscriptionSource.subscription_id,
                )
                .where(Subscription.creator_id.in_(ids))
            )
        ).all()
    )

    source_by_creator: dict[UUID, list[SourceCreator]] = {item.id: [] for item in creators}
    links_by_creator: dict[UUID, list[CreatorLink]] = {item.id: [] for item in creators}
    repositories_by_creator: dict[UUID, list[SubscriptionSource]] = {
        item.id: [] for item in creators
    }
    for row in source_rows:
        if row.creator_id in source_by_creator:
            source_by_creator[row.creator_id].append(row)
    for row in link_rows:
        if row.creator_id in links_by_creator:
            links_by_creator[row.creator_id].append(row)
    for row, creator_id in repository_rows:
        repositories_by_creator[creator_id].append(row)

    observed = 0
    malformed = 0
    for creator in creators:
        links = links_by_creator[creator.id]
        malformed += sum(
            1
            for link in links
            if (link.notes or "").strip().startswith("Danbooru artist tag:")
            and not extract_danbooru_note_aliases(link.notes)
        )
        observations = collect_creator_alias_observations(
            creator,
            source_creators=source_by_creator[creator.id],
            links=links,
            subscription_sources=repositories_by_creator[creator.id],
        )
        current_refs = tuple(
            dict.fromkeys(
                item.source_ref
                for item in observations
                if item.source_ref is not None
            )
        )
        stale_ref = CreatorAlias.source_ref.is_(None)
        if current_refs:
            stale_ref = or_(
                stale_ref,
                CreatorAlias.source_ref.not_in(current_refs),
            )
        await db.execute(
            update(CreatorAlias)
            .where(
                CreatorAlias.creator_id == creator.id,
                CreatorAlias.is_current.is_(True),
                stale_ref,
            )
            .values(is_current=False, last_seen_at=func.now())
        )
        await observe_creator_aliases(
            db,
            creator.id,
            observations,
            request_projection=request_projection,
        )
        observed += len(observations)

    return {
        "creators": len(creators),
        "observations": observed,
        "malformed_danbooru_notes": malformed,
    }


async def backfill_all_creator_aliases(
    db: AsyncSession,
    *,
    page_size: int = 100,
    progress_cb=None,
    request_projection: bool = True,
) -> dict[str, int]:
    from app.services.operations import (
        current_admin_operation_attempt,
        get_current_admin_operation_checkpoint,
        set_current_admin_operation_checkpoint,
    )

    checkpoint_name = "creator_alias_backfill"
    delivery = current_admin_operation_attempt()
    checkpoint = (
        await get_current_admin_operation_checkpoint(db, checkpoint_name)
        if delivery is not None
        else None
    )
    if checkpoint is None:
        high_water = (
            await db.execute(
                select(Creator.created_at, Creator.id)
                .order_by(Creator.created_at.desc(), Creator.id.desc())
                .limit(1)
            )
        ).one_or_none()
        if high_water is None:
            return {
                "scanned": 0,
                "total": 0,
                "pages": 0,
                "observations": 0,
                "malformed_danbooru_notes": 0,
            }
        high_created_at, high_id = high_water
        total = int(
            await db.scalar(
                select(func.count())
                .select_from(Creator)
                .where(
                    tuple_(Creator.created_at, Creator.id)
                    <= tuple_(high_created_at, high_id)
                )
            )
            or 0
        )
        after: tuple[datetime, UUID] | None = None
        scanned = 0
        pages = 0
        observations = 0
        malformed = 0
    else:
        high = checkpoint["high_water"]
        high_created_at = datetime.fromisoformat(str(high["created_at"]))
        high_id = UUID(str(high["id"]))
        raw_after = checkpoint.get("after")
        after = (
            (
                datetime.fromisoformat(str(raw_after["created_at"])),
                UUID(str(raw_after["id"])),
            )
            if isinstance(raw_after, dict)
            else None
        )
        total = int(checkpoint.get("total") or 0)
        scanned = int(checkpoint.get("scanned") or 0)
        pages = int(checkpoint.get("pages") or 0)
        observations = int(checkpoint.get("observations") or 0)
        malformed = int(checkpoint.get("malformed_danbooru_notes") or 0)

    size = max(1, min(int(page_size), 500))
    while True:
        statement = (
            select(Creator.created_at, Creator.id)
            .where(
                tuple_(Creator.created_at, Creator.id)
                <= tuple_(high_created_at, high_id)
            )
            .order_by(Creator.created_at, Creator.id)
            .limit(size)
        )
        if after is not None:
            statement = statement.where(
                tuple_(Creator.created_at, Creator.id) > tuple_(after[0], after[1])
            )
        page = list((await db.execute(statement)).all())
        if not page:
            break

        batch = await backfill_creator_alias_batch(
            db,
            (row.id for row in page),
            request_projection=request_projection,
        )
        scanned += batch["creators"]
        observations += batch["observations"]
        malformed += batch["malformed_danbooru_notes"]
        pages += 1
        last = page[-1]
        after = (last.created_at, last.id)
        progress = {
            "phase": "running",
            "current": scanned,
            "scanned": scanned,
            "total": total,
            "pages": pages,
            "observations": observations,
            "malformed_danbooru_notes": malformed,
        }
        if delivery is not None:
            await set_current_admin_operation_checkpoint(
                db,
                checkpoint_name,
                {
                    "version": 1,
                    "high_water": {
                        "created_at": high_created_at.isoformat(),
                        "id": str(high_id),
                    },
                    "after": {
                        "created_at": after[0].isoformat(),
                        "id": str(after[1]),
                    },
                    **{key: value for key, value in progress.items() if key != "phase"},
                },
                progress=progress,
            )
            await db.commit()
        if progress_cb is not None:
            callback = progress_cb(progress)
            if inspect.isawaitable(callback):
                await callback

    if delivery is not None:
        await set_current_admin_operation_checkpoint(
            db,
            checkpoint_name,
            None,
            progress={
                "phase": "complete",
                "current": scanned,
                "total": total,
            },
        )
        await db.commit()
    return {
        "scanned": scanned,
        "total": total,
        "pages": pages,
        "observations": observations,
        "malformed_danbooru_notes": malformed,
    }
