"""Unified compound-search execution and indexing.

Callers supply raw text and a scope to :class:`SearchService`.  The service is
the single public seam: it parses once, resolves entity identifiers once, then
dispatches the resulting AST to either the Meilisearch or SQL adapter.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, time, timezone
import json
import logging
from pathlib import PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlparse
from uuid import UUID, uuid4

from meilisearch_python_sdk import Client as MeiliClient
from meilisearch_python_sdk.models.settings import MeilisearchSettings
from sqlalchemy import String, and_, cast, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import (
    Asset,
    AssetSource,
    Creator,
    DownloadJob,
    ImportJob,
    SourceCreator,
    Subscription,
    SubscriptionSource,
    Tag,
    TaskRun,
    Work,
    WorkCurationState,
    WorkSource,
    WorkSourceTag,
    WorkTag,
)
from app.providers import registry
from app.services.search_language import (
    HAS_TARGETS,
    IS_TARGETS,
    TYPE_TARGETS,
    SearchDiagnostic,
    SearchQualifier,
    SearchQuery,
    SearchQueryError,
    SearchScope,
    SearchTarget,
    compose_search_query,
    parse_search_query,
    qualifier_catalog,
)
from app.services.tasks import task_payload

logger = logging.getLogger(__name__)


def _validate_index_namespace(database_url: str, index_prefix: str) -> str:
    database_name = urlparse(database_url).path.lstrip("/").split("?", 1)[0]
    if database_name.endswith("_test") and not index_prefix:
        raise RuntimeError(
            "Test database search isolation failure: MEILI_INDEX_PREFIX must be set "
            "before a *_test database may access Meilisearch."
        )
    return index_prefix


_INDEX_PREFIX = _validate_index_namespace(
    settings.database_url,
    settings.meili_index_prefix,
)
WORKS_INDEX = f"{_INDEX_PREFIX}works"
CREATORS_INDEX = f"{_INDEX_PREFIX}creators"
TAGS_INDEX = f"{_INDEX_PREFIX}tags"
REPOSITORIES_INDEX = f"{_INDEX_PREFIX}repositories"
SUBSCRIPTIONS_INDEX = f"{_INDEX_PREFIX}subscriptions"

INDEX_LABELS = {
    WORKS_INDEX: "works",
    CREATORS_INDEX: "creators",
    TAGS_INDEX: "tags",
    REPOSITORIES_INDEX: "repositories",
    SUBSCRIPTIONS_INDEX: "subscriptions",
}

MEILI_TARGET_INDEX: dict[SearchTarget, str] = {
    "works": WORKS_INDEX,
    "creators": CREATORS_INDEX,
    "tags": TAGS_INDEX,
    "repositories": REPOSITORIES_INDEX,
    "subscriptions": SUBSCRIPTIONS_INDEX,
}

INDEX_SETTINGS = {
    WORKS_INDEX: {
        "searchableAttributes": ["title", "description", "creator_names", "tags", "source_work_ids"],
        "filterableAttributes": [
            "id",
            "is_nsfw",
            "is_ai_generated",
            "is_favorite",
            "visibility",
            "sources",
            "tags",
            "creator_ids",
            "repository_ids",
            "has_tags",
            "has_description",
            "has_multiple_assets",
            "has_image",
            "has_animation",
            "has_video",
            "posted_ts",
            "created_ts",
            "updated_ts",
        ],
        "sortableAttributes": ["posted_ts", "created_ts", "updated_ts", "title"],
        "pagination": {"maxTotalHits": 100_000},
    },
    CREATORS_INDEX: {
        "searchableAttributes": ["name", "display_name", "description", "source_creator_ids"],
        "filterableAttributes": [
            "id",
            "is_active",
            "is_favorite",
            "has_subscription",
            "has_repository",
            "has_danbooru",
            "sources",
            "created_ts",
            "updated_ts",
        ],
        "sortableAttributes": ["name_sort", "created_ts", "updated_ts"],
        "pagination": {"maxTotalHits": 100_000},
    },
    TAGS_INDEX: {
        "searchableAttributes": ["normalized_name", "category"],
        "filterableAttributes": ["id", "normalized_name", "category", "created_ts", "updated_ts"],
        "sortableAttributes": ["normalized_name", "usage_count", "created_ts", "updated_ts"],
        "pagination": {"maxTotalHits": 100_000},
    },
    REPOSITORIES_INDEX: {
        "searchableAttributes": [
            "name",
            "creator_name",
            "source",
            "source_creator_id",
            "source_url",
            "subscription_name",
        ],
        "filterableAttributes": [
            "id",
            "creator_id",
            "subscription_id",
            "source",
            "is_enabled",
            "auth_healthy",
            "has_last_sync",
            "has_source_creator_id",
            "created_ts",
            "updated_ts",
            "synced_ts",
        ],
        "sortableAttributes": ["name_sort", "created_ts", "updated_ts", "synced_ts"],
        "pagination": {"maxTotalHits": 100_000},
    },
    SUBSCRIPTIONS_INDEX: {
        "searchableAttributes": ["name", "creator_name", "sources", "source_urls", "source_creator_ids"],
        "filterableAttributes": [
            "id",
            "creator_id",
            "repository_ids",
            "sources",
            "is_active",
            "sync_enabled",
            "never_synced",
            "has_last_sync",
            "created_ts",
            "updated_ts",
            "synced_ts",
        ],
        "sortableAttributes": ["name_sort", "created_ts", "updated_ts", "synced_ts"],
        "pagination": {"maxTotalHits": 100_000},
    },
}

TARGET_PERMISSION = {
    "works": "library",
    "creators": "library",
    "tags": "library",
    "repositories": "subscriptions",
    "subscriptions": "subscriptions",
    "tasks": "tasks",
    "scheduler": "tasks",
}


def _can_search_target(target: SearchTarget, permissions: set[str]) -> bool:
    required = TARGET_PERMISSION[target]
    if required == "library":
        return bool({"library", "curation"} & permissions)
    return required in permissions

DEFAULT_SORT = {
    "works": "created_ts:desc",
    "creators": "name_sort:asc",
    "tags": "usage_count:desc",
    "repositories": "updated_ts:desc",
    "subscriptions": "updated_ts:desc",
}

SORT_FIELD = {
    "posted-desc": ("posted_ts", "desc"),
    "posted-asc": ("posted_ts", "asc"),
    "created-desc": ("created_ts", "desc"),
    "created-asc": ("created_ts", "asc"),
    "updated-desc": ("updated_ts", "desc"),
    "updated-asc": ("updated_ts", "asc"),
    "name-asc": ("name_sort", "asc"),
    "name-desc": ("name_sort", "desc"),
    "usage-desc": ("usage_count", "desc"),
    "last-sync-desc": ("synced_ts", "desc"),
    "last-sync-asc": ("synced_ts", "asc"),
    "title-asc": ("title", "asc"),
    "title-desc": ("title", "desc"),
}

MEILI_FIELD = {
    "works": {
        "repo": "repository_ids",
        "creator": "creator_ids",
        "tag": "tags",
        "source": "sources",
        "posted": "posted_ts",
        "created": "created_ts",
        "updated": "updated_ts",
    },
    "creators": {
        "creator": "id",
        "source": "sources",
        "created": "created_ts",
        "updated": "updated_ts",
    },
    "tags": {
        "tag": "normalized_name",
        "created": "created_ts",
        "updated": "updated_ts",
    },
    "repositories": {
        "repo": "id",
        "creator": "creator_id",
        "source": "source",
        "created": "created_ts",
        "updated": "updated_ts",
        "synced": "synced_ts",
    },
    "subscriptions": {
        "repo": "repository_ids",
        "creator": "creator_id",
        "source": "sources",
        "created": "created_ts",
        "updated": "updated_ts",
        "synced": "synced_ts",
    },
}

IS_FIELD = {
    "works": {
        "favorite": ("is_favorite", True),
        "nsfw": ("is_nsfw", True),
        "sfw": ("is_nsfw", False),
        "ai": ("is_ai_generated", True),
        "human": ("is_ai_generated", False),
        "visible": ("visibility", "visible"),
        "trashed": ("visibility", "trashed"),
    },
    "creators": {
        "favorite": ("is_favorite", True),
        "active": ("is_active", True),
        "inactive": ("is_active", False),
    },
    "repositories": {
        "enabled": ("is_enabled", True),
        "disabled": ("is_enabled", False),
        "auth-ok": ("auth_healthy", True),
        "auth-error": ("auth_healthy", False),
    },
    "subscriptions": {
        "active": ("is_active", True),
        "inactive": ("is_active", False),
        "sync-enabled": ("sync_enabled", True),
        "sync-disabled": ("sync_enabled", False),
        "never-synced": ("never_synced", True),
    },
}

HAS_FIELD = {
    "works": {
        "tags": "has_tags",
        "description": "has_description",
        "multiple-assets": "has_multiple_assets",
        "image": "has_image",
        "animation": "has_animation",
        "video": "has_video",
    },
    "creators": {
        "subscription": "has_subscription",
        "repository": "has_repository",
        "danbooru": "has_danbooru",
    },
    "repositories": {
        "last-sync": "has_last_sync",
        "source-creator-id": "has_source_creator_id",
    },
    "subscriptions": {
        "last-sync": "has_last_sync",
    },
}


class SearchBackendUnavailable(RuntimeError):
    pass


class SearchPermissionError(PermissionError):
    def __init__(self, target: str):
        super().__init__(f"Missing permission for search target: {target}")
        self.target = target


def _client() -> MeiliClient:
    return MeiliClient(settings.meili_url, settings.meili_master_key)


def _wait_for_task(
    client: MeiliClient,
    task: Any,
    *,
    timeout_in_ms: int = 120_000,
    raise_for_status: bool = True,
) -> Any:
    task_uid = getattr(task, "task_uid", None)
    if task_uid is None:
        return task
    return client.wait_for_task(
        task_uid,
        timeout_in_ms=timeout_in_ms,
        raise_for_status=raise_for_status,
    )


def _ensure_indexes(client: MeiliClient, settings_map: dict[str, dict] | None = None) -> None:
    for uid, config in (settings_map or INDEX_SETTINGS).items():
        try:
            index = client.get_index(uid)
        except Exception:
            _wait_for_task(client, client.create_index(uid, primary_key="id"))
            index = client.get_index(uid)
        if index.primary_key is None:
            _wait_for_task(client, index.update(primary_key="id"))
        _wait_for_task(
            client,
            client.index(uid).update_settings(MeilisearchSettings.model_validate(config)),
        )


def _replace_indexes_atomically(
    client: MeiliClient,
    documents_by_index: dict[str, list[dict]],
) -> None:
    """Replace derived projections without exposing a partially rebuilt index."""

    generation = uuid4().hex[:12]
    staging_indexes: dict[str, str] = {}
    selected_settings = {uid: INDEX_SETTINGS[uid] for uid in documents_by_index}

    for live_index, documents in documents_by_index.items():
        staging = f"{live_index}__staging_{generation}"
        staging_indexes[live_index] = staging
        try:
            _wait_for_task(client, client.index(staging).delete())
        except Exception:
            pass
        _wait_for_task(client, client.create_index(staging, primary_key="id"))
        _wait_for_task(
            client,
            client.index(staging).update_settings(
                MeilisearchSettings.model_validate(INDEX_SETTINGS[live_index])
            ),
        )
        if documents:
            _wait_for_task(client, client.index(staging).add_documents(documents))

    _ensure_indexes(client, selected_settings)
    swaps = [(live, staging) for live, staging in staging_indexes.items()]
    try:
        _wait_for_task(client, client.swap_indexes(swaps))
    except Exception:
        # Keep correctness on older Meilisearch/SDK combinations that do not
        # expose atomic swaps. Every task is still awaited before returning.
        logger.warning("Atomic index swap unavailable; rebuilding live indexes", exc_info=True)
        for live, documents in documents_by_index.items():
            _wait_for_task(client, client.index(live).delete_all_documents())
            _wait_for_task(
                client,
                client.index(live).update_settings(
                    MeilisearchSettings.model_validate(INDEX_SETTINGS[live])
                ),
            )
            if documents:
                _wait_for_task(client, client.index(live).add_documents(documents))
    finally:
        for staging in staging_indexes.values():
            try:
                _wait_for_task(client, client.index(staging).delete(), raise_for_status=False)
            except Exception:
                logger.debug(
                    "Failed to remove temporary search index %s",
                    staging,
                    exc_info=True,
                )


def _delete_document(index_name: str, document_id: str) -> None:
    client = _client()
    _wait_for_task(client, client.index(index_name).delete_document(document_id))


def _delete_all_documents(index_name: str) -> None:
    client = _client()
    _wait_for_task(client, client.index(index_name).delete_all_documents())


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _timestamp(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _normalize_url(value: str | None) -> str:
    return (value or "").strip().rstrip("/").lower()


def _meili_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _date_bounds(raw: str) -> tuple[str, int, int | None]:
    operator = "="
    value = raw
    for candidate in ("<=", ">=", "<", ">", "="):
        if raw.startswith(candidate):
            operator = candidate
            value = raw[len(candidate):]
            break
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    start = int(parsed.timestamp())
    if "T" not in value.upper():
        end = int(datetime.combine(parsed.date(), time.max, tzinfo=parsed.tzinfo).timestamp())
    else:
        end = None
    return operator, start, end


def _date_expression(field: str, raw: str) -> str:
    operator, start, end = _date_bounds(raw)
    if operator == "=" and end is not None:
        return f"({field} >= {start} AND {field} <= {end})"
    return f"{field} {operator} {start}"


def _sql_date_expression(column, raw: str):
    operator, start, end = _date_bounds(raw)
    start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end, tz=timezone.utc) if end else None
    if operator == "=" and end_dt:
        return and_(column >= start_dt, column <= end_dt)
    if operator == ">":
        return column > start_dt
    if operator == ">=":
        return column >= start_dt
    if operator == "<":
        return column < start_dt
    return column <= start_dt


def _apply_sql_sort(stmt, query: SearchQuery, model):
    selected = query.values("sort")
    value = selected[0] if selected else "created-desc"
    if value.startswith("updated-"):
        column = model.updated_at
    else:
        column = model.created_at
    return stmt.order_by(column.asc() if value.endswith("-asc") else column.desc())


def _free_text(query: SearchQuery) -> str:
    values = []
    for term in query.terms:
        if term.quoted:
            values.append(json.dumps(term.value, ensure_ascii=False))
        else:
            values.append(term.value)
    return " ".join(values)


def _grouped_qualifiers(query: SearchQuery, target: SearchTarget) -> dict[tuple[str, bool], list[SearchQualifier]]:
    grouped: dict[tuple[str, bool], list[SearchQualifier]] = defaultdict(list)
    for token in query.qualifiers:
        if token.key in {"type", "sort"}:
            continue
        if token.key == "is" and target not in IS_TARGETS.get(token.value, frozenset()):
            continue
        if token.key == "has" and target not in HAS_TARGETS.get(token.value, frozenset()):
            continue
        grouped[(token.key, token.negated)].append(token)
    return grouped


def _resolved_value(token: SearchQualifier, resolved: dict[tuple[str, str], str]) -> str:
    return resolved.get((token.key, token.value), token.value)


def _compile_meili_filter(
    query: SearchQuery,
    target: SearchTarget,
    resolved: dict[tuple[str, str], str],
    *,
    force_sfw: bool,
) -> str | None:
    parts: list[str] = []
    fields = MEILI_FIELD[target]
    for (key, negated), tokens in _grouped_qualifiers(query, target).items():
        expressions: list[str] = []
        if key == "is":
            for token in tokens:
                field, value = IS_FIELD[target][token.value]
                expressions.append(f"{field} {'!=' if negated else '='} {_meili_literal(value)}")
        elif key == "has":
            for token in tokens:
                field = HAS_FIELD[target][token.value]
                expressions.append(f"{field} = {'false' if negated else 'true'}")
        elif key in {"posted", "created", "updated", "synced"}:
            for token in tokens:
                expression = _date_expression(fields[key], token.value)
                expressions.append(f"NOT ({expression})" if negated else expression)
        elif key in fields:
            field = fields[key]
            for token in tokens:
                value = _resolved_value(token, resolved)
                expressions.append(f"{field} {'!=' if negated else '='} {_meili_literal(value)}")
        if expressions:
            # Positive values of the same qualifier are alternatives. Negative
            # values all have to be absent.
            parts.append(f"({' AND '.join(expressions)})" if negated else f"({' OR '.join(expressions)})")

    if target == "works":
        visibility_values = {
            token.value
            for token in query.qualifiers
            if token.key == "is" and not token.negated and token.value in {"visible", "trashed"}
        }
        if not visibility_values:
            parts.append('visibility = "visible"')
        if force_sfw:
            parts.append("is_nsfw = false")
    return " AND ".join(parts) or None


def _meili_sort(query: SearchQuery, target: SearchTarget) -> list[str] | None:
    selected = query.values("sort")
    if selected and selected[0] != "relevance":
        field, direction = SORT_FIELD[selected[0]]
        return [f"{field}:{direction}"]
    if query.terms or selected == ("relevance",):
        return None
    default = DEFAULT_SORT.get(target)
    return [default] if default else None


def _search_hits(result: Any) -> tuple[list[dict], int]:
    return (
        list(getattr(result, "hits", []) or []),
        int(getattr(result, "estimated_total_hits", 0) or 0),
    )


def _diagnostic(code: str, message: str, token: SearchQualifier, suggestions: Iterable[str] = ()) -> SearchQueryError:
    return SearchQueryError(SearchDiagnostic(
        code=code,
        message=message,
        start=token.start,
        end=token.end,
        token=f"{'-' if token.negated else ''}{token.key}:{token.value}",
        suggestions=tuple(suggestions),
    ))


class SearchService:
    """Deep search module used by every internal search surface."""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _allowed_targets(query: SearchQuery, permissions: set[str]) -> tuple[SearchTarget, ...]:
        allowed = tuple(
            target
            for target in query.targets
            if _can_search_target(target, permissions)
            or (
                query.scope == "creator-picker"
                and target == "creators"
                and "upload" in permissions
            )
        )
        explicit_types = query.values("type")
        if explicit_types:
            requested = {TYPE_TARGETS[value] for value in explicit_types}
            denied = requested - set(allowed)
            if denied:
                raise SearchPermissionError(sorted(denied)[0])
        if not allowed:
            raise SearchPermissionError(query.targets[0])
        return allowed

    async def _resolve_creator(self, value: str, token: SearchQualifier) -> tuple[str, list[str]]:
        try:
            creator_id = UUID(value)
        except ValueError:
            creator_id = None
        if creator_id:
            row = await self.db.get(Creator, creator_id)
            if row:
                return str(row.id), []

        normalized = value.strip().lower()
        rows = await self.db.execute(
            select(Creator.id, Creator.name, Creator.display_name)
            .outerjoin(SourceCreator, SourceCreator.creator_id == Creator.id)
            .where(or_(
                func.lower(Creator.name) == normalized,
                func.lower(func.coalesce(Creator.display_name, "")) == normalized,
                func.lower(SourceCreator.source_creator_id) == normalized,
            ))
            .distinct()
            .limit(6)
        )
        matches = rows.all()
        if len(matches) == 1:
            return str(matches[0][0]), []
        suggestions = [
            f'creator:"{display or name}"'
            for _id, name, display in matches[:5]
        ]
        if len(matches) > 1:
            raise _diagnostic("ambiguous_value", f"Creator value is ambiguous: {value}", token, suggestions)

        candidates = await self.db.execute(
            select(Creator.name, Creator.display_name)
            .where(or_(
                Creator.name.ilike(f"%{value}%"),
                Creator.display_name.ilike(f"%{value}%"),
            ))
            .limit(5)
        )
        suggestions = [f'creator:"{display or name}"' for name, display in candidates.all()]
        raise _diagnostic("unknown_value", f"Creator was not found: {value}", token, suggestions)

    async def _resolve_repository(self, value: str, token: SearchQualifier) -> tuple[str, list[str]]:
        try:
            repository_id = UUID(value)
        except ValueError:
            repository_id = None
        if repository_id:
            row = await self.db.get(SubscriptionSource, repository_id)
            if row:
                return str(row.id), []

        normalized = value.strip().rstrip("/").lower()
        source = None
        handle = normalized
        if "/" in normalized and not normalized.startswith(("http://", "https://")):
            source, handle = normalized.split("/", 1)
            source = "x" if source == "twitter" else source
        rows = await self.db.execute(
            select(SubscriptionSource.id, SubscriptionSource.source, SubscriptionSource.source_creator_id, SubscriptionSource.source_url)
            .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
            .join(Creator, Creator.id == Subscription.creator_id)
            .where(or_(
                func.lower(func.coalesce(SubscriptionSource.source_url, "")) == normalized,
                and_(
                    func.lower(func.coalesce(SubscriptionSource.source_creator_id, "")) == handle,
                    SubscriptionSource.source == source,
                ) if source else func.lower(func.coalesce(SubscriptionSource.source_creator_id, "")) == normalized,
                func.lower(func.coalesce(Subscription.name, "")) == normalized,
                func.lower(Creator.name) == normalized,
                func.lower(func.coalesce(Creator.display_name, "")) == normalized,
            ))
            .distinct()
            .limit(6)
        )
        matches = rows.all()
        if len(matches) == 1:
            return str(matches[0][0]), []
        suggestions = [
            f"repo:{source_name}/{source_creator_id}" if source_creator_id else f"repo:{repo_id}"
            for repo_id, source_name, source_creator_id, _url in matches[:5]
        ]
        if len(matches) > 1:
            raise _diagnostic("ambiguous_value", f"Repository value is ambiguous: {value}", token, suggestions)

        candidates = await self.db.execute(
            select(SubscriptionSource.id, SubscriptionSource.source, SubscriptionSource.source_creator_id)
            .where(or_(
                SubscriptionSource.source_creator_id.ilike(f"%{value}%"),
                SubscriptionSource.source_url.ilike(f"%{value}%"),
            ))
            .limit(5)
        )
        suggestions = [
            f"repo:{source_name}/{source_creator_id}" if source_creator_id else f"repo:{repo_id}"
            for repo_id, source_name, source_creator_id in candidates.all()
        ]
        raise _diagnostic("unknown_value", f"Repository was not found: {value}", token, suggestions)

    async def _resolve_tag(self, value: str, token: SearchQualifier) -> tuple[str, list[str]]:
        row = await self.db.execute(
            select(Tag.normalized_name).where(func.lower(Tag.normalized_name) == value.strip().lower()).limit(1)
        )
        match = row.scalar_one_or_none()
        if match:
            return match, []
        candidates = await self.db.execute(
            select(Tag.normalized_name).where(Tag.normalized_name.ilike(f"%{value}%")).limit(5)
        )
        suggestions = [f'tag:"{name}"' for name in candidates.scalars().all()]
        raise _diagnostic("unknown_value", f"Tag was not found: {value}", token, suggestions)

    async def _resolve_qualifiers(self, query: SearchQuery) -> dict[tuple[str, str], str]:
        resolved: dict[tuple[str, str], str] = {}
        for token in query.qualifiers:
            key = (token.key, token.value)
            if key in resolved:
                continue
            if token.key == "creator":
                resolved[key], _ = await self._resolve_creator(token.value, token)
            elif token.key == "repo":
                resolved[key], _ = await self._resolve_repository(token.value, token)
            elif token.key == "tag":
                resolved[key], _ = await self._resolve_tag(token.value, token)
        return resolved

    async def search(
        self,
        query: str,
        offset: int = 0,
        limit: int = 20,
        *,
        scope: SearchScope = "global",
        permissions: set[str] | None = None,
        force_sfw: bool = False,
        kind: str | None = None,
    ) -> dict:
        # ``kind`` remains a thin adapter for internal callers while all
        # execution still crosses the same parsed seam.
        if kind and kind != "all":
            scope = {
                "works": "works",
                "creators": "creators",
                "tags": "tags",
                "repositories": "repositories",
                "subscriptions": "subscriptions",
            }.get(kind, scope)  # type: ignore[assignment]
        parsed = parse_search_query(query, scope)
        permission_set = permissions or {"library", "subscriptions", "tasks", "curation"}
        targets = self._allowed_targets(parsed, permission_set)
        if parsed.values("is") and "trashed" in parsed.values("is") and "curation" not in permission_set:
            raise SearchPermissionError("works:trashed")
        resolved = await self._resolve_qualifiers(parsed)

        groups: dict[str, dict] = {}
        meili_targets = [target for target in targets if target in MEILI_TARGET_INDEX]
        if meili_targets:
            groups.update(await self._search_meili(parsed, meili_targets, resolved, offset, limit, force_sfw))
        if "tasks" in targets:
            groups["tasks"] = await self._search_tasks(parsed, resolved, offset, limit)
        if "scheduler" in targets:
            groups["scheduler"] = await self._search_scheduler(parsed, resolved, offset, limit)

        first_target = targets[0]
        total = groups.get(first_target, {}).get("total", 0) if len(targets) == 1 else groups.get("works", {}).get("total", 0)
        response = {
            "query": query,
            "canonical_query": parsed.canonical,
            "parsed": parsed.payload(),
            "groups": groups,
            "total": total,
            # Compatibility views are derived from the new groups. They do not
            # execute a legacy search path.
            "results": groups.get("works", {}).get("items", []),
            "creators": groups.get("creators", {}).get("items", []),
            "tags": groups.get("tags", {}).get("items", []),
            "repositories": groups.get("repositories", {}).get("items", []),
            "subscriptions": groups.get("subscriptions", {}).get("items", []),
        }
        return response

    async def _search_meili(
        self,
        query: SearchQuery,
        targets: list[SearchTarget],
        resolved: dict[tuple[str, str], str],
        offset: int,
        limit: int,
        force_sfw: bool,
    ) -> dict[str, dict]:
        text = _free_text(query)

        def _run() -> dict[str, dict]:
            client = _client()
            output = {}
            for target in targets:
                index = MEILI_TARGET_INDEX[target]
                target_limit = min(limit, 10) if query.scope == "global" and len(targets) > 1 else limit
                kwargs: dict[str, Any] = {
                    "offset": offset if len(targets) == 1 else 0,
                    "limit": target_limit,
                }
                filter_expression = _compile_meili_filter(query, target, resolved, force_sfw=force_sfw)
                sort = _meili_sort(query, target)
                if filter_expression:
                    kwargs["filter"] = filter_expression
                if sort:
                    kwargs["sort"] = sort
                result = client.index(index).search(text, **kwargs)
                hits, total = _search_hits(result)
                output[target] = {"total": total, "items": hits}
            return output

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:
            logger.warning("Meilisearch search failed", exc_info=True)
            raise SearchBackendUnavailable("Search index is unavailable") from exc

    @staticmethod
    def _task_term_expression(value: str):
        pattern = f"%{value}%"
        return or_(
            cast(TaskRun.id, String).ilike(pattern),
            cast(TaskRun.subject_id, String).ilike(pattern),
            TaskRun.title.ilike(pattern),
            TaskRun.operation_type.ilike(pattern),
            TaskRun.source_url.ilike(pattern),
            TaskRun.error_log.ilike(pattern),
        )

    async def _search_tasks(
        self,
        query: SearchQuery,
        resolved: dict[tuple[str, str], str],
        offset: int,
        limit: int,
    ) -> dict:
        conditions = [TaskRun.kind != "account"]
        for term in query.terms:
            conditions.append(self._task_term_expression(term.value))
        for (key, negated), tokens in _grouped_qualifiers(query, "tasks").items():
            expressions = []
            for token in tokens:
                value = _resolved_value(token, resolved)
                if key == "status":
                    expression = TaskRun.status == ("enqueued" if value == "pending" else value)
                elif key == "kind":
                    expression = TaskRun.kind == value
                elif key == "source":
                    expression = TaskRun.source == value
                elif key == "repo":
                    expression = TaskRun.meta.op("->>")("subscription_source_id") == value
                elif key == "creator":
                    expression = TaskRun.meta.op("->>")("creator_id") == value
                elif key in {"created", "updated"}:
                    operator, start, end = _date_bounds(value)
                    column = TaskRun.created_at if key == "created" else TaskRun.updated_at
                    start_dt = datetime.fromtimestamp(start, tz=timezone.utc)
                    end_dt = datetime.fromtimestamp(end, tz=timezone.utc) if end else None
                    if operator == "=" and end_dt:
                        expression = and_(column >= start_dt, column <= end_dt)
                    elif operator == ">":
                        expression = column > start_dt
                    elif operator == ">=":
                        expression = column >= start_dt
                    elif operator == "<":
                        expression = column < start_dt
                    else:
                        expression = column <= start_dt
                else:
                    continue
                expressions.append(not_(expression) if negated else expression)
            if expressions:
                conditions.append(and_(*expressions) if negated else or_(*expressions))

        stmt = select(TaskRun).where(and_(*conditions))
        count_stmt = select(func.count(TaskRun.id)).where(and_(*conditions))
        sort = query.values("sort")
        if sort and sort[0] in SORT_FIELD:
            field, direction = SORT_FIELD[sort[0]]
            column = TaskRun.created_at if field == "created_ts" else TaskRun.updated_at
            stmt = stmt.order_by(column.asc() if direction == "asc" else column.desc())
        else:
            stmt = stmt.order_by(TaskRun.created_at.desc())
        total = int((await self.db.execute(count_stmt)).scalar_one())
        rows = (await self.db.execute(stmt.offset(offset).limit(limit))).scalars().all()
        return {"total": total, "items": [task_payload(row) for row in rows]}

    async def search_download_jobs(
        self,
        query: str,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> list[DownloadJob]:
        """Return download-domain rows using the canonical task search AST.

        The task page keeps richer download controls, but its text and visual
        filters still cross the same parser/resolver seam as ``/search``.
        """

        parsed = parse_search_query(query, "tasks")
        resolved = await self._resolve_qualifiers(parsed)
        conditions = []
        for term in parsed.terms:
            pattern = f"%{term.value}%"
            conditions.append(or_(
                cast(DownloadJob.id, String).ilike(pattern),
                DownloadJob.source_url.ilike(pattern),
                DownloadJob.error_log.ilike(pattern),
                Subscription.name.ilike(pattern),
                Creator.name.ilike(pattern),
                Creator.display_name.ilike(pattern),
            ))
        for (key, negated), tokens in _grouped_qualifiers(parsed, "tasks").items():
            expressions = []
            for token in tokens:
                value = _resolved_value(token, resolved)
                if key == "status":
                    expression = DownloadJob.status == ("enqueued" if value == "pending" else value)
                elif key == "kind":
                    expression = value == "download"
                elif key == "source":
                    expression = DownloadJob.source == value
                elif key == "repo":
                    expression = DownloadJob.subscription_source_id == UUID(value)
                elif key == "creator":
                    expression = Subscription.creator_id == UUID(value)
                elif key in {"created", "updated"}:
                    column = DownloadJob.created_at if key == "created" else DownloadJob.updated_at
                    expression = _sql_date_expression(column, value)
                else:
                    continue
                expressions.append(not_(expression) if negated else expression)
            if expressions:
                conditions.append(and_(*expressions) if negated else or_(*expressions))

        stmt = (
            select(DownloadJob)
            .join(Subscription, DownloadJob.subscription_id == Subscription.id)
            .join(Creator, Subscription.creator_id == Creator.id)
        )
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = _apply_sql_sort(stmt, parsed, DownloadJob).offset(offset).limit(limit)
        return list((await self.db.execute(stmt)).scalars().unique().all())

    async def search_import_jobs(
        self,
        query: str,
        *,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[int, list[ImportJob]]:
        """Return import-domain rows using the canonical task search AST."""

        parsed = parse_search_query(query, "tasks")
        resolved = await self._resolve_qualifiers(parsed)
        conditions = []
        for term in parsed.terms:
            pattern = f"%{term.value}%"
            conditions.append(or_(
                cast(ImportJob.id, String).ilike(pattern),
                cast(ImportJob.download_job_id, String).ilike(pattern),
                ImportJob.error_log.ilike(pattern),
                DownloadJob.source_url.ilike(pattern),
                Subscription.name.ilike(pattern),
                Creator.name.ilike(pattern),
                Creator.display_name.ilike(pattern),
            ))
        for (key, negated), tokens in _grouped_qualifiers(parsed, "tasks").items():
            expressions = []
            for token in tokens:
                value = _resolved_value(token, resolved)
                if key == "status":
                    expression = ImportJob.status == ("enqueued" if value == "pending" else value)
                elif key == "kind":
                    expression = value == "import"
                elif key == "source":
                    expression = DownloadJob.source == value
                elif key == "repo":
                    expression = DownloadJob.subscription_source_id == UUID(value)
                elif key == "creator":
                    expression = Subscription.creator_id == UUID(value)
                elif key in {"created", "updated"}:
                    column = ImportJob.created_at if key == "created" else ImportJob.updated_at
                    expression = _sql_date_expression(column, value)
                else:
                    continue
                expressions.append(not_(expression) if negated else expression)
            if expressions:
                conditions.append(and_(*expressions) if negated else or_(*expressions))

        base = (
            select(ImportJob)
            .join(DownloadJob, ImportJob.download_job_id == DownloadJob.id)
            .join(Subscription, DownloadJob.subscription_id == Subscription.id)
            .join(Creator, Subscription.creator_id == Creator.id)
        )
        count_stmt = (
            select(func.count(ImportJob.id))
            .join(DownloadJob, ImportJob.download_job_id == DownloadJob.id)
            .join(Subscription, DownloadJob.subscription_id == Subscription.id)
            .join(Creator, Subscription.creator_id == Creator.id)
        )
        if conditions:
            base = base.where(and_(*conditions))
            count_stmt = count_stmt.where(and_(*conditions))
        total = int((await self.db.execute(count_stmt)).scalar_one())
        rows = (
            await self.db.execute(
                _apply_sql_sort(base, parsed, ImportJob).offset(offset).limit(limit)
            )
        ).scalars().unique().all()
        return total, list(rows)

    async def _search_scheduler(
        self,
        query: SearchQuery,
        resolved: dict[tuple[str, str], str],
        offset: int,
        limit: int,
    ) -> dict:
        from app.jobs.subscription_sync import schedule_decision_snapshot
        from app.services.settings import get_scheduler_config
        from zoneinfo import ZoneInfo

        config = await get_scheduler_config(self.db)
        tz_name = config.get("timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        now = datetime.now(tz)
        scheduler_enabled = bool(config.get("scheduler_enabled", True))
        rows = (await self.db.execute(
            select(SubscriptionSource, Subscription, Creator)
            .join(Subscription, SubscriptionSource.subscription_id == Subscription.id)
            .join(Creator, Subscription.creator_id == Creator.id)
            .order_by(Creator.display_name, Creator.name, SubscriptionSource.source)
        )).all()
        items = []
        for repository, subscription, creator in rows:
            try:
                provider = registry.get(repository.source)
                normalized_url = provider.normalize_url(repository.source_url) if repository.source_url else None
                url_valid = bool((normalized_url or repository.source_url) and provider.validate_url(normalized_url or repository.source_url))
                can_download = bool(provider.capabilities.can_download)
                display_name = provider.display_name
            except KeyError:
                url_valid = False
                can_download = False
                display_name = repository.source
            decision = schedule_decision_snapshot(
                subscription,
                config,
                repository.last_synced_at,
                repository.last_attempted_at,
                now,
                tz,
            )
            due = bool(decision.get("due"))
            reason = str(decision.get("reason"))
            auth_healthy = repository.auth_healthy is not False
            if not scheduler_enabled:
                due, reason = False, "scheduler_disabled"
            elif not subscription.is_active:
                due, reason = False, "subscription_inactive"
            elif not subscription.sync_enabled:
                due, reason = False, "subscription_sync_disabled"
            elif not repository.is_enabled:
                due, reason = False, "source_disabled"
            elif not auth_healthy:
                due, reason = False, "auth_unhealthy"
            elif not can_download:
                due, reason = False, "provider_not_downloadable"
            elif not url_valid:
                due, reason = False, "url_invalid"
            items.append({
                "subscription_id": str(subscription.id),
                "subscription_name": subscription.name,
                "subscription_active": subscription.is_active,
                "subscription_sync_enabled": subscription.sync_enabled,
                "creator_id": str(creator.id),
                "creator_name": creator.display_name or creator.name,
                "source_id": str(repository.id),
                "source": repository.source,
                "source_display_name": display_name,
                "source_url": repository.source_url,
                "source_creator_id": repository.source_creator_id,
                "source_enabled": repository.is_enabled,
                "effective_mode": decision.get("mode") or subscription.schedule_mode or config.get("schedule_mode", "interval"),
                "timezone": tz_name,
                "scheduled_times": subscription.scheduled_times or config.get("scheduled_times", ""),
                "sync_interval_hours": subscription.sync_interval_hours,
                "last_synced_at": _iso(repository.last_synced_at),
                "last_attempted_at": _iso(repository.last_attempted_at),
                "due": due,
                "decision": "due_now" if due else reason,
                "reason": reason,
                "next_due_at": decision.get("next_due_at"),
                "window_start": decision.get("window_start"),
                "window_end": decision.get("window_end"),
                "auth_healthy": auth_healthy,
                "url_valid": url_valid,
                "can_download": can_download,
            })

        positive = _grouped_qualifiers(query, "scheduler")
        filtered = []
        for item in items:
            if query.terms:
                haystack = " ".join(str(item.get(key) or "") for key in (
                    "creator_name", "source", "source_url", "source_creator_id", "subscription_name", "source_id",
                )).lower()
                if not all(term.value.lower() in haystack for term in query.terms):
                    continue
            accepted = True
            for (key, negated), tokens in positive.items():
                matches = []
                for token in tokens:
                    value = _resolved_value(token, resolved)
                    if key == "source":
                        match = item["source"] == value
                    elif key == "repo":
                        match = item["source_id"] == value
                    elif key == "creator":
                        match = item["creator_id"] == value
                    elif key == "is":
                        match = {
                            "due": item["due"],
                            "blocked": item["reason"] in {"auth_unhealthy", "url_invalid", "provider_not_downloadable"},
                            "waiting": item["reason"] == "interval_not_due",
                            "manual": item["reason"] == "manual_mode",
                            "disabled": not item["source_enabled"],
                        }.get(token.value, False)
                    else:
                        continue
                    matches.append(not match if negated else match)
                if matches and not (all(matches) if negated else any(matches)):
                    accepted = False
                    break
            if accepted:
                filtered.append(item)
        return {"total": len(filtered), "items": filtered[offset:offset + limit]}

    async def assist(
        self,
        *,
        before_cursor: str,
        after_cursor: str,
        scope: SearchScope,
        limit: int,
        permissions: set[str],
        compose: dict | None = None,
        composes: list[dict] | None = None,
    ) -> dict:
        query = before_cursor + after_cursor
        edits = ([compose] if compose else []) + (composes or [])
        for edit in edits:
            parsed = compose_search_query(
                query,
                scope,
                key=edit["key"],
                value=edit.get("value"),
                operation=edit.get("operation", "set"),
                negated=bool(edit.get("negated")),
                replace_values=edit.get("replace_values") or (),
            )
            query = parsed.canonical
            before_cursor, after_cursor = query, ""

        diagnostic = None
        try:
            parsed = parse_search_query(query, scope)
            self._allowed_targets(parsed, permissions)
        except SearchQueryError as error:
            parsed = None
            diagnostic = error.diagnostic.payload()
        except SearchPermissionError as error:
            parsed = None
            diagnostic = {
                "code": "permission_denied",
                "message": str(error),
                "start": 0,
                "end": len(query),
                "token": query,
                "suggestions": [],
            }

        fragment_start = len(before_cursor)
        while fragment_start > 0 and not before_cursor[fragment_start - 1].isspace():
            fragment_start -= 1
        fragment = before_cursor[fragment_start:]
        suggestions = await self._suggestions(
            fragment=fragment,
            prefix=before_cursor[:fragment_start],
            suffix=after_cursor,
            scope=scope,
            limit=limit,
            permissions=permissions,
        )
        if diagnostic and not suggestions:
            suggestions = [
                {
                    "kind": "repair",
                    "label": suggestion,
                    "description": diagnostic["message"],
                    "query": f"{before_cursor[:fragment_start]}{suggestion}{after_cursor}".strip(),
                }
                for suggestion in diagnostic.get("suggestions", [])[:limit]
            ]
        return {
            "query": query,
            "canonical_query": parsed.canonical if parsed else None,
            "parsed": parsed.payload() if parsed else None,
            "diagnostics": [diagnostic] if diagnostic else [],
            "suggestions": suggestions,
            "catalog": qualifier_catalog(scope),
        }

    async def _suggestions(
        self,
        *,
        fragment: str,
        prefix: str,
        suffix: str,
        scope: SearchScope,
        limit: int,
        permissions: set[str],
    ) -> list[dict]:
        negated = fragment.startswith("-")
        value = fragment[1:] if negated else fragment
        separator = value.find(":")
        if separator < 0:
            key_prefix = value.lower()
            entries = []
            for item in qualifier_catalog(scope):
                key = item["key"]
                if key.startswith(key_prefix):
                    if key == "type":
                        values = [
                            value
                            for value in item["values"]
                            if _can_search_target(TYPE_TARGETS[value], permissions)
                        ]
                        if not values:
                            continue
                    replacement = f"{'-' if negated and item['negatable'] else ''}{key}:"
                    entries.append({
                        "kind": "qualifier",
                        "label": replacement,
                        "description": f"Filter with {key}:",
                        "query": f"{prefix}{replacement}{suffix}",
                    })
            return entries[:limit]

        key = value[:separator].lower()
        partial = value[separator + 1:].strip('"')
        catalog = {item["key"]: item for item in qualifier_catalog(scope)}
        if key not in catalog:
            return []
        values = [candidate for candidate in catalog[key]["values"] if candidate.startswith(partial.lower())]
        if key == "type":
            values = [
                value
                for value in values
                if _can_search_target(TYPE_TARGETS[value], permissions)
            ]
        elif key == "tag":
            if not {"library", "curation"} & permissions:
                return []
            rows = await self.db.execute(
                select(Tag.normalized_name)
                .where(Tag.normalized_name.ilike(f"%{partial}%"))
                .order_by(Tag.normalized_name)
                .limit(limit)
            )
            values = list(rows.scalars().all())
        elif key == "creator":
            if not (
                {"library", "curation"} & permissions
                or (scope == "creator-picker" and "upload" in permissions)
            ):
                return []
            rows = await self.db.execute(
                select(Creator.display_name, Creator.name)
                .where(or_(
                    Creator.display_name.ilike(f"%{partial}%"),
                    Creator.name.ilike(f"%{partial}%"),
                ))
                .order_by(Creator.display_name, Creator.name)
                .limit(limit)
            )
            values = [display or name for display, name in rows.all()]
        elif key == "repo":
            if "subscriptions" not in permissions:
                return []
            rows = await self.db.execute(
                select(SubscriptionSource.source, SubscriptionSource.source_creator_id, SubscriptionSource.id)
                .where(or_(
                    SubscriptionSource.source_creator_id.ilike(f"%{partial}%"),
                    SubscriptionSource.source_url.ilike(f"%{partial}%"),
                ))
                .limit(limit)
            )
            values = [f"{source}/{source_creator_id}" if source_creator_id else str(repo_id) for source, source_creator_id, repo_id in rows.all()]
        replacements = []
        for candidate in values[:limit]:
            rendered = candidate
            if any(char.isspace() for char in candidate):
                rendered = json.dumps(candidate, ensure_ascii=False)
            replacement = f"{'-' if negated else ''}{key}:{rendered}"
            replacements.append({
                "kind": "value",
                "label": replacement,
                "description": f"Use {key}:{candidate}",
                "query": f"{prefix}{replacement}{suffix}".strip(),
            })
        return replacements

    async def _repository_lookup(self) -> tuple[dict[tuple[str, str], list[str]], dict[str, list[str]]]:
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
        return by_identity, by_url

    async def _build_work_documents(self, work_ids: Iterable[UUID] | None = None) -> list[dict]:
        stmt = select(Work, WorkCurationState.visibility).outerjoin(
            WorkCurationState, WorkCurationState.work_id == Work.id,
        )
        if work_ids is not None:
            ids = list(work_ids)
            if not ids:
                return []
            stmt = stmt.where(Work.id.in_(ids))
        work_rows = (await self.db.execute(stmt.order_by(Work.created_at.desc()))).all()
        if not work_rows:
            return []
        ids = [work.id for work, _visibility in work_rows]

        tags: dict[str, set[str]] = defaultdict(set)
        tag_rows = (await self.db.execute(
            select(WorkTag.work_id, Tag.normalized_name)
            .join(Tag, Tag.id == WorkTag.tag_id)
            .where(WorkTag.work_id.in_(ids))
        )).all()
        for work_id, name in tag_rows:
            tags[str(work_id)].add(name)
        source_tag_rows = (await self.db.execute(
            select(WorkSource.work_id, Tag.normalized_name)
            .join(WorkSourceTag, WorkSourceTag.work_source_id == WorkSource.id)
            .join(Tag, Tag.id == WorkSourceTag.tag_id)
            .where(WorkSource.work_id.in_(ids))
        )).all()
        for work_id, name in source_tag_rows:
            tags[str(work_id)].add(name)

        sources: dict[str, set[str]] = defaultdict(set)
        source_work_ids: dict[str, set[str]] = defaultdict(set)
        creator_ids: dict[str, set[str]] = defaultdict(set)
        creator_names: dict[str, set[str]] = defaultdict(set)
        repository_ids: dict[str, set[str]] = defaultdict(set)
        work_source_ids: dict[UUID, str] = {}
        source_rows = (await self.db.execute(
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
        )).all()
        repository_by_identity, repository_by_url = await self._repository_lookup()
        for source_id, work_id, source, source_work_id, source_creator_id, source_url, creator_id, source_name, creator_name, creator_display in source_rows:
            key = str(work_id)
            work_source_ids[source_id] = key
            sources[key].add(source)
            source_work_ids[key].add(source_work_id)
            if creator_id:
                creator_ids[key].add(str(creator_id))
            display = creator_display or source_name or creator_name
            if display:
                creator_names[key].add(display)
            if source_creator_id:
                repository_ids[key].update(repository_by_identity.get((source, source_creator_id), []))
            if source_url:
                repository_ids[key].update(repository_by_url.get(_normalize_url(source_url), []))

        asset_ids: dict[str, list[str]] = defaultdict(list)
        asset_mimes: dict[str, set[str]] = defaultdict(set)
        asset_names: dict[str, set[str]] = defaultdict(set)
        asset_rows = (await self.db.execute(
            select(AssetSource.work_source_id, Asset.id, Asset.mime_type, Asset.file_name)
            .join(Asset, Asset.id == AssetSource.asset_id)
            .where(AssetSource.work_source_id.in_(list(work_source_ids)))
            .order_by(AssetSource.work_source_id, AssetSource.ordinal, Asset.file_name)
        )).all() if work_source_ids else []
        for work_source_id, asset_id, mime, file_name in asset_rows:
            work_key = work_source_ids.get(work_source_id)
            if not work_key:
                continue
            if str(asset_id) not in asset_ids[work_key]:
                asset_ids[work_key].append(str(asset_id))
            if mime:
                asset_mimes[work_key].add(mime.lower())
            asset_names[work_key].add((file_name or "").lower())

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
            documents.append({
                "id": key,
                "title": work.title or "",
                "description": (work.description or "")[:1000],
                "creator_name": next(iter(creator_names[key]), ""),
                "creator_names": sorted(creator_names[key]),
                "creator_id": next(iter(creator_ids[key]), ""),
                "creator_ids": sorted(creator_ids[key]),
                "repository_ids": sorted(repository_ids[key]),
                "source": next(iter(sources[key]), "unknown"),
                "sources": sorted(sources[key]),
                "source_work_ids": sorted(source_work_ids[key]),
                "tags": sorted(tags[key]),
                "is_nsfw": bool(work.is_nsfw),
                "is_ai_generated": bool(work.is_ai_generated),
                "is_favorite": bool(work.is_favorite),
                "visibility": visibility or "visible",
                "curation_visibility": visibility or "visible",
                "thumbnail_asset_id": str(work.thumbnail_asset_id) if work.thumbnail_asset_id else (previews[0] if previews else None),
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
            })
        return documents

    async def _build_creator_documents(self) -> list[dict]:
        rows = (await self.db.execute(
            select(Creator).order_by(Creator.created_at.desc())
        )).scalars().all()
        source_rows = (await self.db.execute(
            select(SourceCreator.creator_id, SourceCreator.source, SourceCreator.source_creator_id)
            .where(SourceCreator.creator_id.isnot(None))
        )).all()
        sources: dict[str, set[str]] = defaultdict(set)
        source_ids: dict[str, set[str]] = defaultdict(set)
        for creator_id, source, source_creator_id in source_rows:
            sources[str(creator_id)].add(source)
            source_ids[str(creator_id)].add(source_creator_id)
        sub_rows = (await self.db.execute(
            select(
                Subscription.creator_id,
                func.count(func.distinct(Subscription.id)),
                func.count(SubscriptionSource.id),
                func.max(SubscriptionSource.last_synced_at),
            )
            .outerjoin(SubscriptionSource, SubscriptionSource.subscription_id == Subscription.id)
            .group_by(Subscription.creator_id)
        )).all()
        sub_counts = {
            str(creator_id): (int(sub_count), int(repo_count), last_synced)
            for creator_id, sub_count, repo_count, last_synced in sub_rows
        }
        return [{
            "id": str(creator.id),
            "name": creator.name,
            "name_sort": (creator.display_name or creator.name).lower(),
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
            "created_at": _iso(creator.created_at),
            "updated_at": _iso(creator.updated_at),
            "created_ts": _timestamp(creator.created_at),
            "updated_ts": _timestamp(creator.updated_at),
        } for creator in rows]

    async def _build_tag_documents(self) -> list[dict]:
        usage = (
            select(WorkTag.tag_id.label("tag_id"), func.count(func.distinct(WorkTag.work_id)).label("usage_count"))
            .group_by(WorkTag.tag_id)
            .subquery()
        )
        rows = (await self.db.execute(
            select(Tag, func.coalesce(usage.c.usage_count, 0))
            .outerjoin(usage, usage.c.tag_id == Tag.id)
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

    async def _build_repository_documents(self) -> list[dict]:
        rows = (await self.db.execute(
            select(SubscriptionSource, Subscription, Creator)
            .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
            .join(Creator, Creator.id == Subscription.creator_id)
            .order_by(SubscriptionSource.created_at.desc())
        )).all()
        return [{
            "id": str(repo.id),
            "name": f"{repo.source}/{repo.source_creator_id}" if repo.source_creator_id else (repo.source_url or str(repo.id)),
            "name_sort": (repo.source_creator_id or repo.source_url or str(repo.id)).lower(),
            "source": repo.source,
            "source_creator_id": repo.source_creator_id,
            "source_url": repo.source_url,
            "creator_id": str(creator.id),
            "creator_name": creator.display_name or creator.name,
            "subscription_id": str(subscription.id),
            "subscription_name": subscription.name,
            "is_enabled": bool(repo.is_enabled),
            "auth_healthy": repo.auth_healthy is not False,
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

    async def _build_subscription_documents(self) -> list[dict]:
        rows = (await self.db.execute(
            select(Subscription, Creator)
            .join(Creator, Creator.id == Subscription.creator_id)
            .order_by(Subscription.created_at.desc())
        )).all()
        source_rows = (await self.db.execute(
            select(SubscriptionSource).order_by(SubscriptionSource.created_at)
        )).scalars().all()
        job_rows = (await self.db.execute(
            select(DownloadJob).order_by(DownloadJob.created_at.desc())
        )).scalars().all()
        by_subscription: dict[str, list[SubscriptionSource]] = defaultdict(list)
        jobs_by_subscription: dict[str, list[DownloadJob]] = defaultdict(list)
        for repository in source_rows:
            by_subscription[str(repository.subscription_id)].append(repository)
        for job in job_rows:
            if job.subscription_id:
                jobs_by_subscription[str(job.subscription_id)].append(job)
        documents = []
        for subscription, creator in rows:
            repositories = by_subscription[str(subscription.id)]
            jobs = jobs_by_subscription[str(subscription.id)]
            latest_job = jobs[0] if jobs else None
            latest_sync = max((repo.last_synced_at for repo in repositories if repo.last_synced_at), default=subscription.last_synced_at)
            documents.append({
                "id": str(subscription.id),
                "name": subscription.name or creator.display_name or creator.name,
                "name_sort": (subscription.name or creator.display_name or creator.name).lower(),
                "creator_id": str(creator.id),
                "creator_name": creator.display_name or creator.name,
                "is_active": bool(subscription.is_active),
                "sync_enabled": bool(subscription.sync_enabled),
                "sync_interval_hours": subscription.sync_interval_hours,
                "schedule_mode": subscription.schedule_mode,
                "scheduled_times": subscription.scheduled_times,
                "never_synced": latest_sync is None,
                "has_last_sync": latest_sync is not None,
                "last_synced_at": _iso(latest_sync),
                "repository_ids": [str(repo.id) for repo in repositories],
                "source_count": len(repositories),
                "enabled_source_count": sum(1 for repo in repositories if repo.is_enabled),
                "running_job_count": sum(1 for job in jobs if job.status in {"enqueued", "downloading", "downloaded", "importing"}),
                "failed_job_count": sum(1 for job in jobs if job.status in {"failed", "stale"}),
                "latest_job_id": str(latest_job.id) if latest_job else None,
                "latest_job_status": latest_job.status if latest_job else None,
                "latest_job_created_at": _iso(latest_job.created_at) if latest_job else None,
                "sources": sorted({repo.source for repo in repositories}),
                "source_urls": [repo.source_url for repo in repositories if repo.source_url],
                "source_creator_ids": [repo.source_creator_id for repo in repositories if repo.source_creator_id],
                "created_at": _iso(subscription.created_at),
                "updated_at": _iso(subscription.updated_at),
                "created_ts": _timestamp(subscription.created_at),
                "updated_ts": _timestamp(subscription.updated_at),
                "synced_ts": _timestamp(latest_sync),
            })
        return documents

    async def index_works(self, work_ids: Iterable[UUID]) -> None:
        documents = await self._build_work_documents(work_ids)
        if not documents:
            return
        try:
            def _run() -> None:
                client = _client()
                _ensure_indexes(client)
                _wait_for_task(client, client.index(WORKS_INDEX).add_documents(documents))

            await asyncio.to_thread(_run)
        except Exception:
            logger.warning("Batch work indexing failed", exc_info=True)

    async def refresh_reference_indexes(self) -> None:
        """Refresh creator/tag/repository/subscription projections.

        These collections are small relative to assets and their aggregate
        fields cross several tables, so one centralized rebuild is both safer
        and cheaper than duplicating document assembly in every mutation path.
        """

        documents_by_index = {
            CREATORS_INDEX: await self._build_creator_documents(),
            TAGS_INDEX: await self._build_tag_documents(),
            REPOSITORIES_INDEX: await self._build_repository_documents(),
            SUBSCRIPTIONS_INDEX: await self._build_subscription_documents(),
        }

        def _run() -> None:
            client = _client()
            _replace_indexes_atomically(client, documents_by_index)

        try:
            await asyncio.to_thread(_run)
        except Exception:
            logger.warning("Reference search indexing failed", exc_info=True)

    async def _batch_index_works(self, docs: list[dict]) -> None:
        """Compatibility adapter for imports; new callers should pass IDs.

        Old import batches may not contain the richer fields. Rebuild those
        documents from the database whenever IDs are available.
        """
        ids = []
        for document in docs:
            try:
                ids.append(UUID(str(document["id"])))
            except (KeyError, ValueError, TypeError):
                continue
        if ids:
            await self.index_works(ids)

    async def index_work(
        self,
        work_id: str,
        title: str | None = None,
        description: str | None = None,
        creator_name: str | None = None,
        is_nsfw: bool = False,
        source: str = "unknown",
        tags: list[str] | None = None,
        posted_at: str | None = None,
        created_at: str | None = None,
        **_unused,
    ) -> None:
        await self.index_works([UUID(work_id)])

    async def delete_work(self, work_id: str) -> None:
        try:
            await asyncio.to_thread(_delete_document, WORKS_INDEX, work_id)
        except Exception:
            logger.warning("Failed to delete work %s from search", work_id, exc_info=True)

    async def delete_creator(self, creator_id: str) -> None:
        try:
            await asyncio.to_thread(_delete_document, CREATORS_INDEX, creator_id)
        except Exception:
            logger.warning("Failed to delete creator %s from search", creator_id, exc_info=True)

    async def delete_tag(self, tag_id: str) -> None:
        try:
            await asyncio.to_thread(_delete_document, TAGS_INDEX, tag_id)
        except Exception:
            logger.warning("Failed to delete tag %s from search", tag_id, exc_info=True)

    async def delete_repository(self, repository_id: str) -> None:
        try:
            await asyncio.to_thread(_delete_document, REPOSITORIES_INDEX, repository_id)
        except Exception:
            logger.warning(
                "Failed to delete repository %s from search",
                repository_id,
                exc_info=True,
            )

    async def delete_subscription(self, subscription_id: str) -> None:
        try:
            await asyncio.to_thread(_delete_document, SUBSCRIPTIONS_INDEX, subscription_id)
        except Exception:
            logger.warning(
                "Failed to delete subscription %s from search",
                subscription_id,
                exc_info=True,
            )

    async def delete_all_works(self) -> None:
        try:
            await asyncio.to_thread(_delete_all_documents, WORKS_INDEX)
        except Exception:
            logger.warning("Failed to clear works search index", exc_info=True)

    async def audit_projection(self) -> dict[str, Any]:
        """Compare database-derived documents with the current search projection."""

        documents_by_index = {
            WORKS_INDEX: await self._build_work_documents(),
            CREATORS_INDEX: await self._build_creator_documents(),
            TAGS_INDEX: await self._build_tag_documents(),
            REPOSITORIES_INDEX: await self._build_repository_documents(),
            SUBSCRIPTIONS_INDEX: await self._build_subscription_documents(),
        }

        def _read_ids() -> dict[str, tuple[set[str], int]]:
            client = _client()
            result: dict[str, tuple[set[str], int]] = {}
            for index_name in documents_by_index:
                search_result = client.index(index_name).search(
                    "",
                    limit=100_000,
                    attributes_to_retrieve=["id"],
                )
                hits, total = _search_hits(search_result)
                result[index_name] = (
                    {str(hit["id"]) for hit in hits if hit.get("id") is not None},
                    total,
                )
            return result

        try:
            indexed_ids = await asyncio.to_thread(_read_ids)
        except Exception as exc:
            raise SearchBackendUnavailable("Search index audit is unavailable") from exc

        indexes: dict[str, dict[str, Any]] = {}
        has_drift = False
        for index_name, documents in documents_by_index.items():
            expected = {str(document["id"]) for document in documents}
            actual, indexed_total = indexed_ids[index_name]
            stale = sorted(actual - expected)
            missing = sorted(expected - actual)
            drifted = bool(stale or missing or indexed_total != len(actual))
            has_drift = has_drift or drifted
            indexes[INDEX_LABELS[index_name]] = {
                "database_count": len(expected),
                "index_count": indexed_total,
                "stale_ids": stale,
                "missing_ids": missing,
                "status": "drift" if drifted else "ok",
            }
        return {"status": "drift" if has_drift else "ok", "indexes": indexes}

    async def reindex(self) -> dict:
        builders = {
            WORKS_INDEX: self._build_work_documents,
            CREATORS_INDEX: self._build_creator_documents,
            TAGS_INDEX: self._build_tag_documents,
            REPOSITORIES_INDEX: self._build_repository_documents,
            SUBSCRIPTIONS_INDEX: self._build_subscription_documents,
        }
        stats: dict[str, int] = {}
        try:
            documents_by_index = {}
            for index, builder in builders.items():
                documents_by_index[index] = await builder()
                stats[index] = len(documents_by_index[index])

            def _rebuild() -> None:
                _replace_indexes_atomically(_client(), documents_by_index)

            await asyncio.to_thread(_rebuild)
        except Exception as exc:
            logger.exception("Search reindex failed")
            return {
                "status": "error",
                "message": str(exc),
                **{INDEX_LABELS[name]: count for name, count in stats.items()},
            }
        return {
            "status": "ok",
            "message": ", ".join(
                f"{INDEX_LABELS[name]}={count}" for name, count in stats.items()
            ),
            **{INDEX_LABELS[name]: count for name, count in stats.items()},
        }
