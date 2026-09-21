"""Unified compound-search execution and indexing.

Callers supply raw text and a scope to :class:`SearchService`.  The service is
the single public seam: it parses once, resolves entity identifiers once, then
dispatches the resulting AST to either the Meilisearch or SQL adapter.
"""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, time, timezone
from difflib import SequenceMatcher
import hashlib
import json
import logging
import math
import struct
import tempfile
import time as monotonic_time
import unicodedata
from pathlib import PurePosixPath
from threading import Lock
from typing import Any, Awaitable, BinaryIO, Callable, Iterable
from urllib.parse import parse_qs, unquote, urlparse
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary, WeakValueDictionary

from meilisearch_python_sdk import Client as MeiliClient
from meilisearch_python_sdk.models.search import SearchParams
from meilisearch_python_sdk.models.settings import MeilisearchSettings
from sqlalchemy import String, and_, case, cast, exists, func, literal_column, not_, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import (
    Asset,
    AssetSource,
    Creator,
    CreatorAlias,
    DownloadJob,
    ImportJob,
    SourceCreator,
    SearchIndexState,
    Subscription,
    SubscriptionSource,
    Tag,
    TaskRun,
    UserSubscription,
    UserSubscriptionSource,
    Work,
    WorkCurationState,
    WorkSource,
    WorkSourceTag,
    WorkTag,
)
from app.models.search_projection_outbox import SearchProjectionOutbox
from app.providers import registry
from app.services.cache import (
    INTERACTIVE_LIST_TTL,
    cache_generation,
    cache_get,
    cache_get_with_status,
    cache_key,
    cache_refresh_lock,
    cache_release_lock,
    cache_set,
    cache_try_lock,
)
from app.services.search_projection_outbox import (
    ProjectionEvent,
    complete_projection_versions,
    enqueue_projection_events,
    prune_completed_projection_events,
    replay_projection_events,
)
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
from app.services.search_consistency import search_index_consistency
from app.services.creator_aliases import normalize_creator_alias
from app.services.operations import inaccessible_admin_operation_types_for_permissions
from app.services.source_search_identity import (
    ParsedSourceURL,
    parse_source_identity,
    parse_source_url,
)
from app.services.tasks import (
    import_job_visibility_condition,
    task_payload,
    task_surface_visibility_condition,
)

logger = logging.getLogger(__name__)
_REBUILD_REPLAY_RECORD = struct.Struct(">16sQ")


@dataclass(frozen=True)
class ResolvedSourceURL:
    parsed: ParsedSourceURL
    work_ids: tuple[str, ...] = ()
    creator_ids: tuple[str, ...] = ()
    repository_ids: tuple[str, ...] = ()
    subscription_ids: tuple[str, ...] = ()

    def ids_for(self, target: SearchTarget) -> tuple[str, ...]:
        return {
            "works": self.work_ids,
            "creators": self.creator_ids,
            "repositories": self.repository_ids,
            "subscriptions": self.subscription_ids,
        }.get(target, ())


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
MEMBERSHIPS_INDEX = f"{_INDEX_PREFIX}subscription_memberships_v1"

INDEX_LABELS = {
    WORKS_INDEX: "works",
    CREATORS_INDEX: "creators",
    TAGS_INDEX: "tags",
    REPOSITORIES_INDEX: "repositories",
    SUBSCRIPTIONS_INDEX: "subscriptions",
    MEMBERSHIPS_INDEX: "subscription_memberships",
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
        "searchableAttributes": [
            "title",
            "description",
            "creator_names",
            "alias_names_current",
            "alias_names_historical",
            "alias_identities_current",
            "alias_identities_historical",
            "tags",
            "source_work_ids",
        ],
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
            "source_creator_keys",
            "source_work_keys",
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
        # ``id`` is the deterministic final key for every non-relevance sort.
        # Without it, equal timestamps/titles can move between offset pages.
        "sortableAttributes": ["posted_ts", "created_ts", "updated_ts", "title", "id"],
        "pagination": {"maxTotalHits": 100_000},
        "nonSeparatorTokens": ["_", "@"],
        "typoTolerance": {
            "enabled": True,
            "disableOnAttributes": [
                "alias_identities_current",
                "alias_identities_historical",
                "source_work_ids",
            ],
        },
    },
    CREATORS_INDEX: {
        "searchableAttributes": [
            "name",
            "display_name",
            "alias_names_current",
            "alias_names_historical",
            "alias_identities_current",
            "alias_identities_historical",
            "description",
            "source_creator_ids",
        ],
        "filterableAttributes": [
            "id",
            "is_active",
            "is_favorite",
            "has_subscription",
            "has_repository",
            "has_danbooru",
            "sources",
            "source_creator_keys",
            "created_ts",
            "updated_ts",
        ],
        "sortableAttributes": ["name_sort", "created_ts", "updated_ts", "id"],
        "pagination": {"maxTotalHits": 100_000},
        "nonSeparatorTokens": ["_", "@"],
        "typoTolerance": {
            "enabled": True,
            "disableOnAttributes": [
                "alias_identities_current",
                "alias_identities_historical",
                "source_creator_ids",
            ],
        },
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
            "alias_names_current",
            "alias_names_historical",
            "alias_identities_current",
            "alias_identities_historical",
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
            "source_creator_keys",
            "is_enabled",
            "auth_healthy",
            "auth_state",
            "credential_state",
            "has_last_sync",
            "has_source_creator_id",
            "created_ts",
            "updated_ts",
            "synced_ts",
        ],
        "sortableAttributes": ["name_sort", "created_ts", "updated_ts", "synced_ts", "id"],
        "pagination": {"maxTotalHits": 100_000},
        "nonSeparatorTokens": ["_", "@"],
        "typoTolerance": {
            "enabled": True,
            "disableOnAttributes": [
                "alias_identities_current",
                "alias_identities_historical",
                "source_creator_id",
                "source_url",
            ],
        },
    },
    SUBSCRIPTIONS_INDEX: {
        "searchableAttributes": [
            "name",
            "creator_name",
            "alias_names_current",
            "alias_names_historical",
            "alias_identities_current",
            "alias_identities_historical",
            "sources",
            "source_urls",
            "source_creator_ids",
        ],
        "filterableAttributes": [
            "id",
            "creator_id",
            "repository_ids",
            "sources",
            "source_creator_keys",
            "is_active",
            "sync_enabled",
            "never_synced",
            "has_last_sync",
            "created_ts",
            "updated_ts",
            "synced_ts",
        ],
        "sortableAttributes": ["name_sort", "created_ts", "updated_ts", "synced_ts", "id"],
        "pagination": {"maxTotalHits": 100_000},
        "nonSeparatorTokens": ["_", "@"],
        "typoTolerance": {
            "enabled": True,
            "disableOnAttributes": [
                "alias_identities_current",
                "alias_identities_historical",
                "source_urls",
                "source_creator_ids",
            ],
        },
    },
}

# One physical index for all actors; identity is the private membership UUID.
INDEX_SETTINGS[MEMBERSHIPS_INDEX] = {
    **INDEX_SETTINGS[SUBSCRIPTIONS_INDEX],
    "searchableAttributes": ["name", "canonical_name", *INDEX_SETTINGS[SUBSCRIPTIONS_INDEX]["searchableAttributes"][1:]],
    "filterableAttributes": [*INDEX_SETTINGS[SUBSCRIPTIONS_INDEX]["filterableAttributes"], "user_id", "subscription_id"],
    "sortableAttributes": [*INDEX_SETTINGS[SUBSCRIPTIONS_INDEX]["sortableAttributes"], "subscription_id"],
}

WORK_PROJECTION_VERSION = 4
WORK_HYDRATION_QUERY_COUNT = 4
# The 4 MiB payload bound remains authoritative.  A larger identity window
# amortizes the four indexed hydration scans on high-latency NAS storage; the
# payload splitter still sends only one bounded Meilisearch write at a time.
WORK_DOCUMENT_BATCH_SIZE = 2_000
MEILI_DOCUMENT_PAYLOAD_BYTES = 4 * 1024 * 1024
# NAS-backed Meilisearch can spend more than 30 seconds durably committing a
# control-plane write even when the corresponding task is healthy.  Keep the
# socket open as long as the task-level deadline so a slow response does not
# turn a successful create/update into a failed rebuild and cleanup cycle.
MEILI_WRITE_TIMEOUT_SECONDS = 120
MEILI_TASK_TIMEOUT_MS = 120_000
# Incremental writes share the same NAS-backed LMDB as full rebuilds.  A
# healthy 4 MiB document task can take more than two minutes while Meilisearch
# compacts or batches adjacent tasks; abandoning it after 30 seconds only
# resubmits duplicate work and prevents the durable outbox from converging.
MEILI_INCREMENTAL_TASK_TIMEOUT_MS = 180_000
MEILI_INCREMENTAL_SLICE_TIMEOUT_MS = 240_000
INDEX_SETTINGS_CACHE_TTL = 30 * 24 * 60 * 60
INDEX_WRITE_LOCK = "search:index-write"
INDEX_WRITE_LOCK_TTL_SECONDS = 900


def _parallel_work_hydration_supported() -> bool:
    """Keep one worker connection free for heartbeats and task state."""

    connection_capacity = int(settings.db_pool_size) + int(settings.db_max_overflow)
    return connection_capacity >= WORK_HYDRATION_QUERY_COUNT + 1

# A scoped works search only renders the gallery-card projection.  Avoid
# faulting descriptions and full tag arrays from the mmap-backed index on every
# page while retaining rich documents for global search consumers.
WORK_LIST_RETRIEVE_FIELDS = [
    "id",
    "title",
    "posted_at",
    "created_at",
    "updated_at",
    "posted_ts",
    "created_ts",
    "updated_ts",
    "is_nsfw",
    "is_ai_generated",
    "is_favorite",
    "thumbnail_asset_id",
    "preview_asset_ids",
    "asset_count",
    "source",
    "sources",
    "creator_name",
    "creator_names",
    "creator_id",
    "creator_ids",
    "has_tags",
    "has_description",
    "has_multiple_assets",
    "has_image",
    "has_animation",
    "has_ugoira",
    "has_video",
    "visibility",
    "curation_visibility",
    "projection_version",
    "projection_hash",
]

_count_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_ensured_settings_hashes: set[tuple[str, str]] = set()
_settings_hash_lock = Lock()
_meili_search_semaphores: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    asyncio.BoundedSemaphore,
] = WeakKeyDictionary()
_meili_search_gate_lock = Lock()
_meili_breaker_lock = Lock()
_meili_breaker_failures = 0
_meili_breaker_open_until = 0.0
_meili_breaker_half_open = False
MEILI_SEARCH_CONCURRENCY = 4
MEILI_BREAKER_FAILURE_THRESHOLD = 3
MEILI_BREAKER_OPEN_SECONDS = 30.0

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
    "subscriptions": "name_sort:asc",
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
        "uid": "source_creator_keys",
        "pid": "source_work_keys",
        "posted": "posted_ts",
        "created": "created_ts",
        "updated": "updated_ts",
    },
    "creators": {
        "creator": "id",
        "source": "sources",
        "uid": "source_creator_keys",
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
        "uid": "source_creator_keys",
        "created": "created_ts",
        "updated": "updated_ts",
        "synced": "synced_ts",
    },
    "subscriptions": {
        "repo": "repository_ids",
        "creator": "creator_id",
        "source": "sources",
        "uid": "source_creator_keys",
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
        "auth-ok": ("auth_state", "healthy"),
        "auth-error": ("auth_state", "unhealthy"),
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


class NameAnchorsUnavailable(ValueError):
    """The active reference query cannot expose stable name offsets."""


REFERENCE_NAME_ANCHORS = tuple(
    [
        {"key": chr(code), "label": chr(code), "kind": "latin"}
        for code in range(ord("A"), ord("Z") + 1)
    ]
    + [
        {"key": "0-9", "label": "0–9", "kind": "digit"},
        {"key": "kana", "label": "かな", "kind": "kana"},
        {"key": "han", "label": "汉", "kind": "han"},
        {"key": "other", "label": "#", "kind": "other"},
    ]
)


def _meili_search_semaphore() -> asyncio.BoundedSemaphore:
    """Return one bounded search gate per asyncio event loop."""

    loop = asyncio.get_running_loop()
    with _meili_search_gate_lock:
        semaphore = _meili_search_semaphores.get(loop)
        if semaphore is None:
            semaphore = asyncio.BoundedSemaphore(MEILI_SEARCH_CONCURRENCY)
            _meili_search_semaphores[loop] = semaphore
        return semaphore


def _meili_breaker_before_request() -> None:
    """Fail fast while open and permit exactly one half-open probe."""

    global _meili_breaker_half_open
    now = monotonic_time.monotonic()
    with _meili_breaker_lock:
        if _meili_breaker_open_until > now:
            raise SearchBackendUnavailable("Search index circuit breaker is open")
        if _meili_breaker_open_until:
            if _meili_breaker_half_open:
                raise SearchBackendUnavailable("Search index recovery probe is in progress")
            _meili_breaker_half_open = True


def _meili_breaker_success() -> None:
    global _meili_breaker_failures, _meili_breaker_open_until, _meili_breaker_half_open
    with _meili_breaker_lock:
        _meili_breaker_failures = 0
        _meili_breaker_open_until = 0.0
        _meili_breaker_half_open = False


def _meili_breaker_failure() -> None:
    global _meili_breaker_failures, _meili_breaker_open_until, _meili_breaker_half_open
    now = monotonic_time.monotonic()
    with _meili_breaker_lock:
        _meili_breaker_failures += 1
        if (
            _meili_breaker_half_open
            or _meili_breaker_failures >= MEILI_BREAKER_FAILURE_THRESHOLD
        ):
            _meili_breaker_open_until = now + MEILI_BREAKER_OPEN_SECONDS
        _meili_breaker_half_open = False


def meili_search_circuit_status() -> dict[str, Any]:
    """Cheap process-local breaker snapshot for system health integration."""

    now = monotonic_time.monotonic()
    with _meili_breaker_lock:
        remaining = max(0.0, _meili_breaker_open_until - now)
        return {
            "status": "open" if remaining > 0 else (
                "half_open" if _meili_breaker_half_open else "closed"
            ),
            "consecutive_failures": _meili_breaker_failures,
            "retry_after_seconds": round(remaining, 3),
            "concurrency_limit": MEILI_SEARCH_CONCURRENCY,
        }


class SearchPermissionError(PermissionError):
    def __init__(self, target: str):
        super().__init__(f"Missing permission for search target: {target}")
        self.target = target


def _client(*, timeout_seconds: float | None = None) -> MeiliClient:
    """Create an SDK client whose socket lifetime is actually bounded.

    ``asyncio.wait_for(asyncio.to_thread(...))`` cannot stop a blocking worker
    thread.  Passing a timeout into the SDK makes the underlying httpx request
    close first, so repeated Meilisearch stalls cannot accumulate orphaned
    threads in the backend executor.
    """

    timeout = timeout_seconds
    if timeout is None:
        timeout = MEILI_WRITE_TIMEOUT_SECONDS
    return MeiliClient(
        settings.meili_url,
        settings.meili_master_key,
        timeout=max(1, int(math.ceil(timeout))),
    )


def _meili_document_count(index_uid: str) -> int:
    stats = _client(timeout_seconds=MEILI_WRITE_TIMEOUT_SECONDS).index(index_uid).get_stats()
    if isinstance(stats, dict):
        value = stats.get("numberOfDocuments", stats.get("number_of_documents", 0))
    else:
        value = getattr(stats, "number_of_documents", getattr(stats, "numberOfDocuments", 0))
    return int(value or 0)


async def _refresh_search_index_checkpoints(index_uids: Iterable[str]) -> None:
    """Persist an exact generation/count checkpoint after a drained writer."""

    models = {
        WORKS_INDEX: Work,
        CREATORS_INDEX: Creator,
        TAGS_INDEX: Tag,
        REPOSITORIES_INDEX: SubscriptionSource,
        SUBSCRIPTIONS_INDEX: Subscription,
        MEMBERSHIPS_INDEX: UserSubscription,
    }
    for index_uid in tuple(dict.fromkeys(index_uids)):
        model = models.get(index_uid)
        if model is None:
            continue
        try:
            index_count = await asyncio.to_thread(_meili_document_count, index_uid)
            async with async_session() as checkpoint_db:
                state = (
                    await checkpoint_db.execute(
                        select(SearchIndexState)
                        .where(SearchIndexState.index_uid == index_uid)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                pending = bool(
                    (
                        await checkpoint_db.execute(
                            select(
                                exists().where(
                                    SearchProjectionOutbox.index_uid == index_uid,
                                    SearchProjectionOutbox.completed_at.is_(None),
                                )
                            )
                        )
                    ).scalar_one()
                )
                if state is None:
                    state = SearchIndexState(
                        index_uid=index_uid,
                        database_generation=1,
                        indexed_generation=0,
                        status="catching_up",
                    )
                    checkpoint_db.add(state)
                    await checkpoint_db.flush()
                if pending:
                    state.status = "catching_up"
                    await checkpoint_db.commit()
                    continue
                database_count = int(
                    (
                        await checkpoint_db.execute(
                            select(func.count()).select_from(model)
                        )
                    ).scalar_one()
                )
                state.database_document_count = database_count
                state.index_document_count = index_count
                state.indexed_generation = state.database_generation
                state.status = "ready" if database_count == index_count else "drift"
                state.last_verified_at = datetime.now(timezone.utc)
                state.last_error = None if database_count == index_count else "document_count_mismatch"
                await checkpoint_db.commit()
        except Exception as exc:
            logger.warning("Unable to refresh search consistency checkpoint for %s", index_uid, exc_info=True)
            try:
                async with async_session() as checkpoint_db:
                    state = (
                        await checkpoint_db.execute(
                            select(SearchIndexState).where(SearchIndexState.index_uid == index_uid)
                        )
                    ).scalar_one_or_none()
                    if state:
                        state.status = "unavailable"
                        state.last_error = str(exc)[:1000]
                        await checkpoint_db.commit()
            except Exception:
                logger.debug("Unable to persist search checkpoint failure", exc_info=True)


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


def _settings_digest(config: dict[str, Any]) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _meili_document_ids_filter(identities: Iterable[str]) -> str:
    """Build a Meilisearch 1.12-compatible primary-key batch filter."""

    return f"id IN {json.dumps(list(identities), ensure_ascii=False)}"


def _index_revision(index: Any) -> str | None:
    # ``updated_at`` also advances for ordinary document writes in Meili and
    # would make every fresh worker mistake content churn for a schema change,
    # re-applying settings on the next batch.  ``created_at`` identifies index
    # replacement while remaining stable across document updates.
    value = getattr(index, "created_at", None) or getattr(index, "updated_at", None)
    return str(value) if value is not None else None


def _index_not_found(error: Exception) -> bool:
    code = getattr(error, "code", None) or getattr(error, "error_code", None)
    return code == "index_not_found" or "index_not_found" in str(error).lower()


def _ensure_indexes(
    client: MeiliClient,
    settings_map: dict[str, dict] | None = None,
    *,
    force_settings: bool = False,
    task_timeout_in_ms: int = MEILI_TASK_TIMEOUT_MS,
) -> None:
    """Create indexes and apply settings only when their schema hash changes."""

    for uid, config in (settings_map or INDEX_SETTINGS).items():
        digest = _settings_digest(config)
        process_key = (uid, digest)
        with _settings_hash_lock:
            process_match = process_key in _ensured_settings_hashes
        if process_match and not force_settings:
            # Normal drains stay entirely off Meilisearch's control plane.
            # A failed document write still surfaces and can clear/recreate the
            # index explicitly; settings do not need a GET on every batch.
            continue

        created = False
        try:
            index = client.get_index(uid)
        except Exception as exc:
            if not _index_not_found(exc):
                raise
            _wait_for_task(
                client,
                client.create_index(uid, primary_key="id"),
                timeout_in_ms=task_timeout_in_ms,
            )
            index = client.get_index(uid)
            created = True
        if index.primary_key is None:
            _wait_for_task(
                client,
                index.update(primary_key="id"),
                timeout_in_ms=task_timeout_in_ms,
            )

        cache_entry = cache_key("search:index-settings", index_uid=uid)
        revision = _index_revision(index)
        cached_settings = cache_get(cache_entry)
        persistent_match = (
            isinstance(cached_settings, dict)
            and cached_settings.get("digest") == digest
            and cached_settings.get("index_revision") == revision
        )
        if not force_settings and not created and persistent_match:
            with _settings_hash_lock:
                _ensured_settings_hashes.add(process_key)
            continue
        _wait_for_task(
            client,
            client.index(uid).update_settings(MeilisearchSettings.model_validate(config)),
            timeout_in_ms=task_timeout_in_ms,
        )
        try:
            revision = _index_revision(client.get_index(uid))
        except Exception:
            # A missing revision forces one safe settings verification in the
            # next process instead of trusting a stale UID-only digest.
            revision = None
        cache_set(
            cache_entry,
            {"digest": digest, "index_revision": revision},
            INDEX_SETTINGS_CACHE_TTL,
        )
        with _settings_hash_lock:
            _ensured_settings_hashes.add(process_key)


def _forget_ensured_settings(index_uids: Iterable[str]) -> None:
    """Force one control-plane verification after a document delivery error."""

    selected = set(index_uids)
    if not selected:
        return
    with _settings_hash_lock:
        _ensured_settings_hashes.difference_update(
            key for key in tuple(_ensured_settings_hashes) if key[0] in selected
        )


def _create_staging_index(client: MeiliClient, live_index: str, staging: str) -> None:
    """Create one empty, fully configured staging index."""

    _wait_for_task(
        client,
        client.create_index(staging, primary_key="id"),
        timeout_in_ms=MEILI_TASK_TIMEOUT_MS,
    )
    _wait_for_task(
        client,
        client.index(staging).update_settings(
            MeilisearchSettings.model_validate(INDEX_SETTINGS[live_index])
        ),
        timeout_in_ms=MEILI_TASK_TIMEOUT_MS,
    )


def _write_document_batch(
    client: MeiliClient,
    index_uid: str,
    documents: list[dict[str, Any]],
    *,
    timeout_in_ms: int = MEILI_TASK_TIMEOUT_MS,
) -> None:
    for batch in _document_batches(documents):
        # Deliberately wait before issuing the next task: at most one LMDB
        # indexing write is in flight, which prevents NAS write amplification.
        _wait_for_task(
            client,
            client.index(index_uid).add_documents(batch),
            timeout_in_ms=timeout_in_ms,
        )


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


def _select_projection_event_slice(
    events: Iterable[ProjectionEvent],
    documents_by_index: dict[str, list[dict[str, Any]]],
    *,
    max_bytes: int = MEILI_DOCUMENT_PAYLOAD_BYTES,
) -> tuple[list[ProjectionEvent], list[ProjectionEvent]]:
    """Choose one FIFO event slice whose aggregate wire payload is bounded."""

    queued = list(events)
    documents = {
        (index_uid, str(document["id"])): document
        for index_uid, index_documents in documents_by_index.items()
        for document in index_documents
    }
    selected: list[ProjectionEvent] = []
    # Reserve array brackets and a small envelope per index/action task.  The
    # estimate is intentionally conservative; the writer performs the exact
    # per-task check again before submitting anything.
    payload_bytes = 64
    for position, event in enumerate(queued):
        document = (
            documents.get((event.index_uid, event.entity_id))
            if event.action == "upsert"
            else None
        )
        payload = document if document is not None else event.entity_id
        event_bytes = len(json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")) + 65
        if payload_bytes + event_bytes > max_bytes:
            if not selected:
                raise ValueError(
                    f"Search projection event {event.index_uid}/{event.entity_id} "
                    f"exceeds the {max_bytes}-byte incremental slice"
                )
            return selected, queued[position:]
        selected.append(event)
        payload_bytes += event_bytes
    return selected, []


def _delete_document_batch(
    client: MeiliClient,
    index_uid: str,
    document_ids: Iterable[str],
    *,
    timeout_in_ms: int = MEILI_TASK_TIMEOUT_MS,
) -> None:
    identities = list(dict.fromkeys(str(value) for value in document_ids))
    if not identities:
        return
    _wait_for_task(
        client,
        client.index(index_uid).delete_documents(identities),
        timeout_in_ms=timeout_in_ms,
    )


def _ensure_live_index_for_swap(client: MeiliClient, live_index: str) -> None:
    """Ensure a swap target exists without running a redundant settings task."""

    try:
        index = client.get_index(live_index)
    except Exception as exc:
        if not _index_not_found(exc):
            raise
        _wait_for_task(
            client,
            client.create_index(live_index, primary_key="id"),
            timeout_in_ms=MEILI_TASK_TIMEOUT_MS,
        )
        return
    if index.primary_key is None:
        _wait_for_task(
            client,
            index.update(primary_key="id"),
            timeout_in_ms=MEILI_TASK_TIMEOUT_MS,
        )


def _remember_live_settings(
    live_indexes: Iterable[str],
    client: MeiliClient | None = None,
) -> None:
    """Record the settings now attached to live UIDs after an atomic swap."""

    for live_index in live_indexes:
        digest = _settings_digest(INDEX_SETTINGS[live_index])
        revision = None
        if client is not None:
            try:
                revision = _index_revision(client.get_index(live_index))
            except Exception:
                logger.debug(
                    "Unable to read index revision after swap for %s",
                    live_index,
                    exc_info=True,
                )
        cache_set(
            cache_key("search:index-settings", index_uid=live_index),
            {
                "digest": digest,
                "index_revision": revision,
            },
            INDEX_SETTINGS_CACHE_TTL,
        )
        with _settings_hash_lock:
            _ensured_settings_hashes.add((live_index, digest))


def _drop_index_best_effort(client: MeiliClient, index_uid: str) -> None:
    try:
        _wait_for_task(
            client,
            client.index(index_uid).delete(),
            timeout_in_ms=MEILI_TASK_TIMEOUT_MS,
            raise_for_status=False,
        )
    except Exception as exc:
        if not _index_not_found(exc):
            logger.debug("Failed to remove temporary search index %s", index_uid, exc_info=True)


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


def _with_projection_hash(document: dict[str, Any], *, version: int = 1) -> dict[str, Any]:
    """Attach a deterministic projection fingerprint for sampled audits."""

    projected = dict(document)
    projected["projection_version"] = version
    payload = json.dumps(
        projected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    projected["projection_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return projected


def _normalize_url(value: str | None) -> str:
    return (value or "").strip().rstrip("/").lower()


def _normalized_url_expression(column):
    return func.lower(func.rtrim(func.btrim(column), "/"))


def _source_url_identity_hint(parsed: ParsedSourceURL) -> str | None:
    url = urlparse(parsed.normalized_url)
    if parsed.source == "danbooru" and parsed.kind == "creator":
        tags = parse_qs(url.query).get("tags")
        if tags:
            return unquote(tags[0])
    parts = [part for part in url.path.split("/") if part]
    if not parts:
        return None
    hint = parts[-2] if parts[-1] in {"article", "pins"} and len(parts) > 1 else parts[-1]
    if parsed.source == "bilibili" and hint.startswith("cv"):
        hint = hint[2:]
    return hint or None


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
    if operator == "<=":
        return column <= start_dt
    return column == start_dt


def _sql_sort_spec(query: SearchQuery, model) -> tuple[Any, str, bool]:
    selected = query.values("sort")
    value = selected[0] if selected else "created-desc"
    field = value.rsplit("-", 1)[0]
    attribute = {
        "created": "created_at",
        "updated": "updated_at",
        "posted": "posted_at",
        "title": "title",
        "name": "name",
        "last-sync": "last_synced_at",
    }.get(field, "created_at")
    column = getattr(model, attribute, model.created_at)
    direction_asc = value.endswith("-asc")
    return column, attribute, direction_asc


def _apply_sql_sort(stmt, query: SearchQuery, model, *, reverse: bool = False):
    column, _attribute, direction_asc = _sql_sort_spec(query, model)
    if reverse:
        direction_asc = not direction_asc
    ordered = column.asc() if direction_asc else column.desc()
    # A deterministic UUID tie-breaker keeps offset pages stable when titles or
    # timestamps collide. Nullable fields need explicit NULL placement for
    # cursor boundaries; nonnullable fields retain the existing index order.
    if column.nullable:
        ordered = ordered.nulls_first() if reverse else ordered.nulls_last()
    identity_order = model.id.asc() if direction_asc else model.id.desc()
    return stmt.order_by(ordered, identity_order)


def _encode_work_cursor(
    query: SearchQuery,
    work: Work,
    *,
    seek: str,
    force_sfw: bool,
) -> str:
    _column, attribute, _ascending = _sql_sort_spec(query, Work)
    boundary = getattr(work, attribute)
    if isinstance(boundary, datetime):
        boundary = boundary.isoformat()
    payload = {
        "v": 1,
        "q": hashlib.sha256(query.canonical.encode("utf-8")).hexdigest()[:16],
        "sfw": bool(force_sfw),
        "sort": (query.values("sort") or ("created-desc",))[0],
        "seek": seek,
        "value": boundary,
        "id": str(work.id),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def _decode_work_cursor(
    cursor: str,
    query: SearchQuery,
    *,
    force_sfw: bool,
) -> tuple[str, Any, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        expected_query = hashlib.sha256(query.canonical.encode("utf-8")).hexdigest()[:16]
        expected_sort = (query.values("sort") or ("created-desc",))[0]
        if (
            payload.get("v") != 1
            or payload.get("q") != expected_query
            or bool(payload.get("sfw")) != bool(force_sfw)
            or payload.get("sort") != expected_sort
            or payload.get("seek") not in {"after", "before"}
        ):
            raise ValueError
        identity = UUID(str(payload["id"]))
        _column, attribute, _ascending = _sql_sort_spec(query, Work)
        value = payload.get("value")
        if value is not None and attribute in {"created_at", "updated_at", "posted_at"}:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        elif value is not None:
            value = str(value)
        return str(payload["seek"]), value, identity
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid or stale works cursor") from exc


def _work_seek_expression(
    query: SearchQuery,
    *,
    seek: str,
    value: Any,
    identity: UUID,
):
    column, _attribute, ascending = _sql_sort_spec(query, Work)
    after = seek == "after"
    forward_identity = Work.id > identity if ascending else Work.id < identity
    backward_identity = Work.id < identity if ascending else Work.id > identity
    identity_expression = forward_identity if after else backward_identity
    if value is None:
        same_null = and_(column.is_(None), identity_expression)
        return same_null if after else or_(column.is_not(None), same_null)

    if ascending:
        value_expression = column > value if after else column < value
    else:
        value_expression = column < value if after else column > value
    same_value = and_(column == value, identity_expression)
    if after:
        # All NULL values sort after every non-NULL value in both directions.
        return or_(value_expression, column.is_(None), same_value)
    return or_(value_expression, same_value)


def _free_text(query: SearchQuery) -> str:
    values = []
    for term in query.terms:
        if term.quoted:
            values.append(json.dumps(term.value, ensure_ascii=False))
        else:
            values.append(term.value)
    return " ".join(values)


def _normalize_reference_name(value: str) -> str:
    """Match PostgreSQL's reference-list normalization in search documents."""

    return unicodedata.normalize("NFKC", value).casefold()


def _alias_projection_fields(aliases: Iterable[Any]) -> dict[str, list[Any]]:
    name_kinds = {"name", "other_name"}
    ordered = sorted(
        aliases,
        key=lambda item: (
            not bool(item.is_current),
            0 if item.kind == "name" else 1 if item.kind == "other_name" else 2,
            str(item.source),
            str(item.normalized_value),
        ),
    )
    fields: dict[str, list[Any]] = {
        "alias_names_current": [],
        "alias_names_historical": [],
        "alias_identities_current": [],
        "alias_identities_historical": [],
        "alias_records": [],
    }
    seen: dict[str, set[str]] = {
        key: set() for key in fields if key != "alias_records"
    }
    for item in ordered:
        family = "names" if item.kind in name_kinds else "identities"
        suffix = "current" if item.is_current else "historical"
        field = f"alias_{family}_{suffix}"
        normalized = str(item.normalized_value)
        if normalized not in seen[field]:
            seen[field].add(normalized)
            fields[field].append(str(item.value))
        fields["alias_records"].append(
            {
                "value": str(item.value),
                "normalized_value": normalized,
                "source": str(item.source),
                "kind": str(item.kind),
                "is_current": bool(item.is_current),
            }
        )
    return fields


def _merge_alias_projection_fields(
    projections: Iterable[dict[str, list[Any]]],
) -> dict[str, list[Any]]:
    merged = _alias_projection_fields(())
    seen = {key: set() for key in merged if key != "alias_records"}
    for projection in projections:
        for key in seen:
            for value in projection.get(key, []):
                normalized = _normalize_reference_name(str(value))
                if normalized not in seen[key]:
                    seen[key].add(normalized)
                    merged[key].append(value)
        merged["alias_records"].extend(projection.get("alias_records", []))
    return merged


_ALIAS_PROJECTION_FIELDS = (
    "alias_names_current",
    "alias_names_historical",
    "alias_identities_current",
    "alias_identities_historical",
    "alias_records",
)


def _strip_alias_projection_fields(hit: dict[str, Any]) -> None:
    for field in _ALIAS_PROJECTION_FIELDS:
        hit.pop(field, None)


def _decorate_alias_hit(hit: dict[str, Any], query_text: str) -> None:
    records = list(hit.get("alias_records") or [])
    query_values = {
        normalize_creator_alias(query_text, kind="name"),
        normalize_creator_alias(query_text, kind="account"),
    } - {""}
    primary_values = {
        normalize_creator_alias(str(hit.get(field) or ""), kind="name")
        for field in ("name", "display_name", "creator_name")
    } - {""}
    if query_values & primary_values:
        _strip_alias_projection_fields(hit)
        return

    candidates: list[tuple[int, bool, int, dict[str, Any]]] = []
    for position, record in enumerate(records):
        normalized = str(record.get("normalized_value") or "")
        if not normalized:
            continue
        if normalized in query_values:
            match_rank = 0
            match_type = "exact"
        elif any(
            normalized.startswith(value) or value.startswith(normalized)
            for value in query_values
            if len(value) >= 2
        ):
            match_rank = 1
            match_type = "prefix"
        elif record.get("kind") in {"name", "other_name"} and max(
            (
                SequenceMatcher(None, value, normalized).ratio()
                for value in query_values
            ),
            default=0.0,
        ) >= 0.72:
            match_rank = 2
            match_type = "fuzzy"
        else:
            continue
        candidates.append(
            (
                match_rank,
                not bool(record.get("is_current")),
                position,
                {**record, "match_type": match_type},
            )
        )
    if candidates:
        selected = min(candidates, key=lambda item: item[:3])[3]
        hit["matched_identity"] = {
            key: selected.get(key)
            for key in (
                "creator_id",
                "value",
                "source",
                "kind",
                "is_current",
                "match_type",
            )
        }
    _strip_alias_projection_fields(hit)


def _reference_name_expressions(name_expression: Any) -> tuple[Any, Any, Any]:
    """Return normalized name, fixed anchor key, and sortable anchor rank."""

    normalized = func.lower(
        func.normalize(name_expression, literal_column("NFKC"))
    )
    initial = func.substr(normalized, 1, 1)
    is_latin = initial.op("~")(r"^[a-z]$")
    is_digit = initial.op("~")(r"^[0-9]$")
    is_kana = initial.op("~")(r"^[\u3040-\u30ff\uff66-\uff9f]$")
    is_han = initial.op("~")(r"^[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]$")
    anchor_key = case(
        (is_latin, func.upper(initial)),
        (is_digit, "0-9"),
        (is_kana, "kana"),
        (is_han, "han"),
        else_="other",
    )
    anchor_rank = case(
        (is_latin, func.ascii(initial) - func.ascii("a")),
        (is_digit, 26),
        (is_kana, 27),
        (is_han, 28),
        else_=29,
    )
    return normalized, anchor_key, anchor_rank


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


def _resolved_value(token: SearchQualifier, resolved: dict[tuple[str, str], Any]) -> str:
    value = resolved.get((token.key, token.value), token.value)
    return value if isinstance(value, str) else token.value


def _resolved_source_url(
    token: SearchQualifier,
    resolved: dict[tuple[str, str], Any],
) -> ResolvedSourceURL | None:
    value = resolved.get((token.key, token.value))
    return value if isinstance(value, ResolvedSourceURL) else None


def _compile_meili_filter(
    query: SearchQuery,
    target: SearchTarget,
    resolved: dict[tuple[str, str], Any],
    *,
    force_sfw: bool,
    identity_field: str = "id",
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
        elif key == "url":
            for token in tokens:
                source_url = _resolved_source_url(token, resolved)
                identities = source_url.ids_for(target) if source_url else ()
                if identities:
                    identity_expression = " OR ".join(
                        f"{identity_field} = {_meili_literal(identity)}" for identity in identities
                    )
                    expression = f"({identity_expression})"
                else:
                    expression = f'{identity_field} = "__source_url_no_match__"'
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
        order = [f"{field}:{direction}"]
        if target in {"works", "creators", "repositories", "subscriptions"} and field != "id":
            order.append(f"id:{direction}")
        return order
    if query.terms or selected == ("relevance",):
        return None
    default = DEFAULT_SORT.get(target)
    if not default:
        return None
    order = [default]
    if target in {"works", "creators", "repositories", "subscriptions"}:
        direction = default.rsplit(":", 1)[-1]
        order.append(f"id:{direction}")
    return order


def _matching_strategy(target: SearchTarget) -> str:
    return "last" if target == "works" else "all"


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

    def __init__(self, db: AsyncSession, *, parallel_hydration: bool = False):
        self.db = db
        self._parallel_hydration = parallel_hydration
        self._repository_maps: tuple[
            dict[tuple[str, str], list[str]],
            dict[str, list[str]],
        ] | None = None

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

    async def _exact_creator_aliases_for_value(
        self,
        value: str,
    ) -> list[CreatorAlias]:
        normalized_values = tuple(
            {
                normalize_creator_alias(value, kind="name"),
                normalize_creator_alias(value, kind="account"),
            }
            - {""}
        )
        if not normalized_values:
            return []
        kind_rank = case(
            (CreatorAlias.kind == "account", 0),
            (CreatorAlias.kind == "url_handle", 1),
            (CreatorAlias.kind == "source_id", 2),
            (CreatorAlias.kind == "url", 3),
            (CreatorAlias.kind == "name", 4),
            else_=5,
        )
        return list(
            (
                await self.db.execute(
                    select(CreatorAlias)
                    .where(CreatorAlias.normalized_value.in_(normalized_values))
                    .order_by(
                        CreatorAlias.is_current.desc(),
                        kind_rank,
                        CreatorAlias.last_seen_at.desc(),
                        CreatorAlias.creator_id,
                    )
                )
            ).scalars()
        )

    async def _exact_creator_aliases(
        self,
        query: SearchQuery,
    ) -> list[CreatorAlias]:
        if len(query.terms) != 1:
            return []
        return await self._exact_creator_aliases_for_value(query.terms[0].value)

    @staticmethod
    def _parse_exact_alias_query(value: str, scope: SearchScope) -> SearchQuery:
        """Represent one stored identity as a literal term.

        Stored source identities may legitimately contain query-language
        punctuation (for example Danbooru aliases with parentheses).  Exact
        PostgreSQL resolution happens before query-language parsing, so once
        an entire input value is known to be an identity it must not be
        reinterpreted as grouping, a qualifier, or multiple natural-language
        terms.
        """

        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        parsed = parse_search_query(f'"{escaped}"', scope)
        return replace(parsed, raw=value)

    @staticmethod
    def _alias_filtered_query(
        query: SearchQuery,
        creator_ids: Iterable[UUID],
    ) -> SearchQuery:
        qualifier_tokens = tuple(query.qualifiers)
        synthetic = tuple(
            SearchQualifier(
                key="creator",
                value=str(creator_id),
                negated=False,
                quoted=False,
                start=0,
                end=0,
            )
            for creator_id in dict.fromkeys(creator_ids)
        )
        return replace(query, tokens=(*qualifier_tokens, *synthetic))

    @staticmethod
    def _attach_exact_alias_matches(
        target: SearchTarget,
        group: dict,
        aliases: Iterable[CreatorAlias],
    ) -> None:
        by_creator: dict[str, CreatorAlias] = {}
        for alias in aliases:
            by_creator.setdefault(str(alias.creator_id), alias)
        for item in group.get("items", []):
            if target == "creators":
                creator_ids = (str(item.get("id") or ""),)
            elif target == "works":
                creator_ids = tuple(str(value) for value in item.get("creator_ids") or ())
            else:
                creator_ids = (str(item.get("creator_id") or ""),)
            alias = next(
                (by_creator[value] for value in creator_ids if value in by_creator),
                None,
            )
            if alias is None:
                continue
            item["matched_identity"] = {
                "creator_id": str(alias.creator_id),
                "value": alias.value,
                "source": alias.source,
                "kind": alias.kind,
                "is_current": bool(alias.is_current),
                "match_type": "exact",
            }

    async def _resolve_creator(self, value: str, token: SearchQualifier) -> tuple[str, list[str]]:
        try:
            creator_id = UUID(value)
        except ValueError:
            creator_id = None
        if creator_id:
            row = await self.db.get(Creator, creator_id)
            if row:
                return str(row.id), []

        normalized = normalize_creator_alias(value, kind="name")
        account_normalized = normalize_creator_alias(value, kind="account")
        rows = await self.db.execute(
            select(Creator.id, Creator.name, Creator.display_name)
            .outerjoin(SourceCreator, SourceCreator.creator_id == Creator.id)
            .where(or_(
                func.lower(func.normalize(Creator.name, literal_column("NFKC"))) == normalized,
                func.lower(
                    func.normalize(
                        func.coalesce(Creator.display_name, ""),
                        literal_column("NFKC"),
                    )
                )
                == normalized,
                func.lower(SourceCreator.source_creator_id) == normalized,
                Creator.id.in_(
                    select(CreatorAlias.creator_id).where(
                        CreatorAlias.normalized_value.in_(
                            tuple({normalized, account_normalized})
                        )
                    )
                ),
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
        normalized = value.strip().lower()
        row = await self.db.execute(
            select(Tag.normalized_name).where(Tag.normalized_name == normalized).limit(1)
        )
        match = row.scalar_one_or_none()
        if match:
            return match, []
        candidates = await self.db.execute(
            select(Tag.normalized_name).where(Tag.normalized_name.ilike(f"%{value}%")).limit(5)
        )
        suggestions = [f'tag:"{name}"' for name in candidates.scalars().all()]
        raise _diagnostic("unknown_value", f"Tag was not found: {value}", token, suggestions)

    async def _resolve_source_url(
        self,
        value: str,
        token: SearchQualifier,
    ) -> ResolvedSourceURL:
        parsed = parse_source_url(value)
        if parsed is None:
            raise _diagnostic(
                "unsupported_url",
                "URL is not a supported creator or work URL.",
                token,
            )
        normalized = _normalize_url(parsed.normalized_url)
        hint = _source_url_identity_hint(parsed)

        if parsed.kind == "work":
            matches = [_normalized_url_expression(WorkSource.source_url) == normalized]
            if hint:
                matches.extend((
                    WorkSource.source_work_id == hint,
                    _normalized_url_expression(WorkSource.source_url).like(
                        f"%/{hint.lower()}"
                    ),
                ))
            rows = (await self.db.execute(
                select(WorkSource.work_id)
                .where(WorkSource.source == parsed.source, or_(*matches))
                .distinct()
            )).scalars().all()
            return ResolvedSourceURL(
                parsed=parsed,
                work_ids=tuple(sorted(str(identity) for identity in rows)),
            )

        pairs: set[tuple[str, str]] = set()
        creator_ids: set[UUID] = set()
        repository_ids: set[UUID] = set()
        subscription_ids: set[UUID] = set()
        work_ids: set[UUID] = set()

        alias_values = {
            normalize_creator_alias(parsed.normalized_url, kind="url"),
        }
        if hint:
            alias_values.add(normalize_creator_alias(hint, kind="account"))
        creator_ids.update(
            (
                await self.db.execute(
                    select(CreatorAlias.creator_id)
                    .where(
                        CreatorAlias.source == parsed.source,
                        CreatorAlias.normalized_value.in_(tuple(alias_values)),
                        CreatorAlias.kind.in_({"url", "url_handle", "account"}),
                    )
                    .distinct()
                )
            ).scalars()
        )

        source_creator_matches = [
            _normalized_url_expression(SourceCreator.source_url) == normalized,
        ]
        repository_matches = [
            _normalized_url_expression(SubscriptionSource.source_url) == normalized,
        ]
        if hint:
            source_creator_matches.append(SourceCreator.source_creator_id == hint)
            if parsed.source in {"x", "iwara", "weibo"}:
                source_creator_matches.extend((
                    func.lower(func.coalesce(SourceCreator.raw_metadata.op("->>")("name"), ""))
                    == hint.lower(),
                    func.lower(func.coalesce(SourceCreator.raw_metadata.op("->>")("screen_name"), ""))
                    == hint.lower(),
                    func.lower(func.coalesce(SourceCreator.raw_metadata.op("->>")("username"), ""))
                    == hint.lower(),
                ))
            repository_matches.append(SubscriptionSource.source_creator_id == hint)

        source_rows = (await self.db.execute(
            select(SourceCreator.source_creator_id, SourceCreator.creator_id)
            .where(
                SourceCreator.source == parsed.source,
                or_(*source_creator_matches),
            )
        )).all()
        for source_creator_id, creator_id in source_rows:
            pairs.add((parsed.source, source_creator_id))
            if creator_id:
                creator_ids.add(creator_id)

        repository_rows = (await self.db.execute(
            select(
                SubscriptionSource.id,
                SubscriptionSource.subscription_id,
                SubscriptionSource.source_creator_id,
                Subscription.creator_id,
            )
            .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
            .where(
                SubscriptionSource.source == parsed.source,
                or_(*repository_matches),
            )
        )).all()
        for repository_id, subscription_id, source_creator_id, creator_id in repository_rows:
            repository_ids.add(repository_id)
            subscription_ids.add(subscription_id)
            creator_ids.add(creator_id)
            if source_creator_id:
                pairs.add((parsed.source, source_creator_id))

        if parsed.source == "danbooru" and "/artists/" in parsed.normalized_url and hint:
            try:
                artist_id = int(hint)
            except ValueError:
                artist_id = None
            if artist_id is not None:
                creator_ids.update((await self.db.execute(
                    select(Creator.id).where(Creator.danbooru_artist_id == artist_id)
                )).scalars().all())

        if creator_ids:
            creator_source_rows = (await self.db.execute(
                select(SourceCreator.source_creator_id)
                .where(
                    SourceCreator.creator_id.in_(creator_ids),
                    SourceCreator.source == parsed.source,
                )
            )).scalars().all()
            pairs.update((parsed.source, identity) for identity in creator_source_rows)

            creator_repository_rows = (await self.db.execute(
                select(
                    SubscriptionSource.id,
                    SubscriptionSource.subscription_id,
                    SubscriptionSource.source_creator_id,
                )
                .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
                .where(
                    Subscription.creator_id.in_(creator_ids),
                    SubscriptionSource.source == parsed.source,
                )
            )).all()
            for repository_id, subscription_id, source_creator_id in creator_repository_rows:
                repository_ids.add(repository_id)
                subscription_ids.add(subscription_id)
                if source_creator_id:
                    pairs.add((parsed.source, source_creator_id))

        if pairs:
            pair_conditions = [
                and_(
                    WorkSource.source == source,
                    WorkSource.source_creator_id == source_creator_id,
                )
                for source, source_creator_id in pairs
            ]
            work_ids.update((await self.db.execute(
                select(WorkSource.work_id).where(or_(*pair_conditions)).distinct()
            )).scalars().all())

            repository_pair_conditions = [
                and_(
                    SubscriptionSource.source == source,
                    SubscriptionSource.source_creator_id == source_creator_id,
                )
                for source, source_creator_id in pairs
            ]
            related_repository_rows = (await self.db.execute(
                select(
                    SubscriptionSource.id,
                    SubscriptionSource.subscription_id,
                    Subscription.creator_id,
                )
                .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
                .where(or_(*repository_pair_conditions))
            )).all()
            for repository_id, subscription_id, creator_id in related_repository_rows:
                repository_ids.add(repository_id)
                subscription_ids.add(subscription_id)
                creator_ids.add(creator_id)

        return ResolvedSourceURL(
            parsed=parsed,
            work_ids=tuple(sorted(str(identity) for identity in work_ids)),
            creator_ids=tuple(sorted(str(identity) for identity in creator_ids)),
            repository_ids=tuple(sorted(str(identity) for identity in repository_ids)),
            subscription_ids=tuple(sorted(str(identity) for identity in subscription_ids)),
        )

    async def _resolve_qualifiers(self, query: SearchQuery) -> dict[tuple[str, str], Any]:
        resolved: dict[tuple[str, str], Any] = {}
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
            elif token.key == "url":
                resolved[key] = await self._resolve_source_url(token.value, token)
        return resolved

    async def _hedged_structured_works_search(
        self,
        query: SearchQuery,
        resolved: dict[tuple[str, str], Any],
        offset: int,
        limit: int,
        *,
        force_sfw: bool,
        cursor: str | None,
    ) -> tuple[dict, dict[str, Any]]:
        """Race an equivalent first page only after the durable gate passes."""

        db_task = asyncio.create_task(
            self._search_works_db(
                query,
                resolved,
                offset,
                limit,
                force_sfw=force_sfw,
                cursor=cursor,
            )
        )
        # Cursor and later offset pages must stay on the authoritative keyset
        # path.  Do not even consult the index gate for those requests.
        if cursor or offset > 0:
            return await db_task, {
                "winner": "postgresql",
                "hedged": False,
                "consistency": "authoritative_page",
                "index_status": "not_eligible",
                "index_lag": None,
            }

        gate_task = asyncio.create_task(search_index_consistency(WORKS_INDEX))
        done, _ = await asyncio.wait({db_task}, timeout=0.075)
        if done:
            gate_task.cancel()
            with suppress(asyncio.CancelledError):
                await gate_task
            return await db_task, {
                "winner": "postgresql",
                "hedged": False,
                "consistency": "authoritative",
                "index_status": "not_needed",
                "index_lag": None,
            }

        # A slow consistency check must never delay an already completed
        # PostgreSQL query.  After the 75 ms threshold, whichever finishes
        # first decides whether a hedge can still add value.
        if not gate_task.done():
            gate_or_db, _ = await asyncio.wait(
                {db_task, gate_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if db_task in gate_or_db:
                gate_task.cancel()
                with suppress(asyncio.CancelledError):
                    await gate_task
                return await db_task, {
                    "winner": "postgresql",
                    "hedged": False,
                    "consistency": "authoritative",
                    "index_status": "gate_slower_than_database",
                    "index_lag": None,
                }
        try:
            gate = await gate_task
        except Exception:
            logger.warning("search index consistency gate failed", exc_info=True)
            return await db_task, {
                "winner": "postgresql",
                "hedged": False,
                "consistency": "gate_unavailable",
                "index_status": "unverified",
                "index_lag": None,
            }
        if not gate["consistent"]:
            return await db_task, {
                "winner": "postgresql",
                "hedged": False,
                "consistency": gate,
                "index_status": gate["status"],
                "index_lag": gate["generation_lag"],
            }

        meili_task = asyncio.create_task(
            self._search_meili(
                query,
                ["works"],
                resolved,
                offset,
                limit,
                force_sfw,
            )
        )

        async def validated_meili_result() -> tuple[dict[str, dict] | None, dict[str, Any]]:
            result = await meili_task
            try:
                validation = await search_index_consistency(WORKS_INDEX)
            except Exception:
                logger.warning("search index post-race validation failed", exc_info=True)
                return None, {**gate, "consistent": False, "status": "unverified"}
            checkpoint_fields = (
                "database_generation",
                "indexed_generation",
                "database_document_count",
                "index_document_count",
            )
            if not validation["consistent"] or any(
                validation.get(field) != gate.get(field)
                for field in checkpoint_fields
            ):
                return None, validation
            return result, validation

        done, _ = await asyncio.wait(
            {db_task, meili_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if meili_task in done:
            try:
                meili_result, validation = await validated_meili_result()
            except SearchBackendUnavailable:
                return await db_task, {
                    "winner": "postgresql",
                    "hedged": True,
                    "consistency": gate,
                    "index_status": "fallback",
                    "index_lag": gate["generation_lag"],
                }
            if meili_result is None:
                return await db_task, {
                    "winner": "postgresql",
                    "hedged": True,
                    "consistency": validation,
                    "index_status": "changed_during_race",
                    "index_lag": validation.get("generation_lag"),
                }
            db_task.cancel()
            with suppress(asyncio.CancelledError):
                await db_task
            return meili_result["works"], {
                "winner": "meilisearch",
                "hedged": True,
                "consistency": gate,
                "index_status": gate["status"],
                "index_lag": gate["generation_lag"],
            }

        try:
            db_result = await db_task
        except Exception as db_error:
            meili_result, validation = await validated_meili_result()
            if meili_result is None:
                raise db_error
            return meili_result["works"], {
                "winner": "meilisearch",
                "hedged": True,
                "consistency": validation,
                "index_status": validation["status"],
                "index_lag": validation["generation_lag"],
            }
        meili_task.cancel()
        with suppress(asyncio.CancelledError, SearchBackendUnavailable):
            await meili_task
        return db_result, {
            "winner": "postgresql",
            "hedged": True,
            "consistency": gate,
            "index_status": gate["status"],
            "index_lag": gate["generation_lag"],
        }

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
        cursor: str | None = None,
        allowed_subscription_ids: set[UUID] | None = None,
        allowed_repository_ids: set[UUID] | None = None,
        user_id: int | None = None,
    ) -> dict:
        request_started_at = monotonic_time.perf_counter()
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
        # A complete stored identity is authoritative and must be resolved
        # before the search language interprets punctuation or whitespace.
        # This also keeps aliases such as ``circle_name_(old)`` searchable
        # without weakening the parser for ordinary free-text expressions.
        raw_exact_aliases = await self._exact_creator_aliases_for_value(query)
        url_input = query.strip().casefold().startswith(("http://", "https://"))
        parsed = (
            self._parse_exact_alias_query(query, scope)
            if raw_exact_aliases and not url_input
            else parse_search_query(query, scope)
        )
        parsed_at = monotonic_time.perf_counter()
        permission_set = permissions if permissions is not None else {"library", "subscriptions", "tasks", "curation"}
        targets = self._allowed_targets(parsed, permission_set)
        if parsed.values("is") and "trashed" in parsed.values("is") and "curation" not in permission_set:
            raise SearchPermissionError("works:trashed")
        resolved = await self._resolve_qualifiers(parsed)
        resolved_at = monotonic_time.perf_counter()

        groups: dict[str, dict] = {}
        execution: dict[str, Any] = {
            "winner": "postgresql",
            "hedged": False,
            "consistency": "authoritative",
            "index_status": "not_needed",
            "index_lag": None,
        }

        exact_aliases = raw_exact_aliases or await self._exact_creator_aliases(parsed)
        exact_alias_targets = tuple(
            target
            for target in targets
            if target in {"works", "creators", "repositories", "subscriptions"}
            and not (target == "subscriptions" and user_id is not None)
        )
        exact_alias_search = bool(exact_aliases and exact_alias_targets)
        if exact_alias_search:
            alias_query = self._alias_filtered_query(
                parsed,
                (alias.creator_id for alias in exact_aliases),
            )
            alias_creator_count = len({alias.creator_id for alias in exact_aliases})
            alias_rank: dict[str, int] = {}
            for position, alias in enumerate(exact_aliases):
                alias_rank.setdefault(str(alias.creator_id), position)
            for target in exact_alias_targets:
                target_offset = offset if len(exact_alias_targets) == 1 else 0
                target_limit = (
                    min(limit, 10) if len(exact_alias_targets) > 1 else limit
                )
                # A unique identity does not need client-side creator ranking.
                # Page it at the requested offset so prolific creators remain
                # searchable beyond the first 1,000 related works.  Ambiguous
                # aliases still fetch a bounded candidate window so current
                # identities can be ranked ahead of historical ones.
                direct_page = alias_creator_count == 1 or bool(parsed.values("sort"))
                fetch_offset = target_offset if direct_page else 0
                fetch_limit = (
                    target_limit
                    if direct_page
                    else min(
                        1000,
                        max(target_offset + target_limit, alias_creator_count * 10),
                    )
                )
                if target == "works":
                    group = await self._search_works_db(
                        alias_query,
                        resolved,
                        fetch_offset,
                        fetch_limit,
                        force_sfw=force_sfw,
                        cursor=cursor,
                    )
                else:
                    group = await self._search_identity_reference_db(
                        target,
                        alias_query,
                        resolved,
                        fetch_offset,
                        fetch_limit,
                        allowed_subscription_ids=allowed_subscription_ids,
                        allowed_repository_ids=allowed_repository_ids,
                        user_id=user_id,
                    )
                self._attach_exact_alias_matches(target, group, exact_aliases)
                if not direct_page:
                    group["items"].sort(
                        key=lambda item: alias_rank.get(
                            str((item.get("matched_identity") or {}).get("creator_id") or ""),
                            len(alias_rank),
                        )
                    )
                    group["items"] = group["items"][
                        target_offset:target_offset + target_limit
                    ]
                groups[target] = group

        # Works list queries with no free text use PostgreSQL.  This keeps the
        # high-traffic gallery independent from the search index while still
        # preserving the public compound-search contract.
        text = _free_text(parsed)
        has_filters = bool(parsed.qualifiers)
        has_source_identity = any(
            token.key in {"uid", "pid", "url"}
            for token in parsed.qualifiers
        )
        if (
            not text
            and len(targets) == 1
            and targets[0] in {"creators", "subscriptions"}
        ):
            target = targets[0]
            groups[target] = await self._search_browse_reference_db(
                target,
                parsed,
                resolved,
                offset,
                limit,
                allowed_subscription_ids=allowed_subscription_ids,
                user_id=user_id,
            )
        elif not text and has_source_identity:
            for target in targets:
                target_offset = offset if len(targets) == 1 else 0
                target_limit = min(limit, 10) if len(targets) > 1 else limit
                if target == "works":
                    groups[target] = await self._search_works_db(
                        parsed,
                        resolved,
                        target_offset,
                        target_limit,
                        force_sfw=force_sfw,
                        cursor=cursor,
                    )
                elif target in {"creators", "repositories", "subscriptions"}:
                    groups[target] = await self._search_identity_reference_db(
                        target,
                        parsed,
                        resolved,
                        target_offset,
                        target_limit,
                        allowed_subscription_ids=allowed_subscription_ids,
                        allowed_repository_ids=allowed_repository_ids,
                        user_id=user_id,
                    )
        elif not text and targets == ("works",) and self._works_db_compatible(parsed):
            groups["works"], execution = await self._hedged_structured_works_search(
                parsed,
                resolved,
                offset,
                limit,
                force_sfw=force_sfw,
                cursor=cursor,
            )
        elif not text and not has_filters and len(targets) == 1:
            target = targets[0]
            if target in ("creators", "subscriptions", "tags", "repositories"):
                groups[target] = await self._search_reference_db(
                    target,
                    offset,
                    limit,
                    allowed_subscription_ids=allowed_subscription_ids,
                    allowed_repository_ids=allowed_repository_ids,
                    user_id=user_id,
                )

        # Meilisearch 1.12 supports attribute-level typo controls but not the
        # later ``disableOnNumbers`` setting.  A single numeric reference term
        # is therefore authoritative in PostgreSQL: an exact stored identity
        # was handled above, while an unknown number must not degrade into a
        # one-digit typo match against another creator identity.
        numeric_identity_literal = (
            not parsed.qualifiers
            and len(parsed.terms) == 1
            and unicodedata.normalize(
                "NFKC",
                parsed.terms[0].value.strip(),
            ).isdecimal()
        )
        if numeric_identity_literal and not exact_alias_search:
            for target in targets:
                if target in {"creators", "repositories", "subscriptions"} and not (target == "subscriptions" and user_id is not None):
                    groups.setdefault(target, {"total": 0, "items": []})

        meili_targets = [
            t for t in targets if t in MEILI_TARGET_INDEX and t not in groups
            and (not exact_alias_search or (t == "subscriptions" and user_id is not None))
        ]
        if cursor and meili_targets:
            raise ValueError("Cursor pagination is only available for structured work lists")
        if meili_targets:
            groups.update(
                await self._search_meili(
                    parsed,
                    meili_targets,
                    resolved,
                    offset,
                    limit,
                    force_sfw,
                    allowed_subscription_ids=allowed_subscription_ids,
                    allowed_repository_ids=allowed_repository_ids,
                    user_id=user_id,
                )
            )
            execution.update({
                "winner": "meilisearch",
                "consistency": "fulltext_index_required",
                "index_status": "required",
            })
        if "tasks" in targets:
            groups["tasks"] = await self._search_tasks(
                parsed,
                resolved,
                offset,
                limit,
                permissions=permission_set,
                user_id=user_id,
            )
        if "scheduler" in targets:
            groups["scheduler"] = await self._search_scheduler(
                parsed,
                resolved,
                offset,
                limit,
                user_id=user_id,
            )

        # Alias projection fields exist only to drive Meilisearch relevance
        # and explain which identity matched.  Never expose the raw projection
        # arrays through the public search response.
        for group in groups.values():
            for item in group.get("items", []):
                _strip_alias_projection_fields(item)

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
            "next_cursor": groups.get("works", {}).get("next_cursor"),
            "previous_cursor": groups.get("works", {}).get("previous_cursor"),
            "creators": groups.get("creators", {}).get("items", []),
            "tags": groups.get("tags", {}).get("items", []),
            "repositories": groups.get("repositories", {}).get("items", []),
            "subscriptions": groups.get("subscriptions", {}).get("items", []),
        }
        completed_at = monotonic_time.perf_counter()
        execution["elapsed_ms"] = round(
            (completed_at - request_started_at) * 1000,
            2,
        )
        response["execution"] = execution
        logger.info(
            "search completed scope=%s targets=%s offset=%d limit=%d backend=%s parse_ms=%.1f resolve_ms=%.1f execute_ms=%.1f total_ms=%.1f",
            scope,
            ",".join(targets),
            offset,
            limit,
            ",".join("postgres" if target in groups and target == "works" and not text and self._works_db_compatible(parsed) else "search" for target in targets),
            (parsed_at - request_started_at) * 1000,
            (resolved_at - parsed_at) * 1000,
            (completed_at - resolved_at) * 1000,
            (completed_at - request_started_at) * 1000,
        )
        return response

    async def _search_meili(
        self,
        query: SearchQuery,
        targets: list[SearchTarget],
        resolved: dict[tuple[str, str], Any],
        offset: int,
        limit: int,
        force_sfw: bool,
        *,
        allowed_subscription_ids: set[UUID] | None = None,
        allowed_repository_ids: set[UUID] | None = None,
        user_id: int | None = None,
    ) -> dict[str, dict]:
        text = _free_text(query)
        timeout_seconds = max(0.1, float(settings.meili_search_timeout_seconds))
        deadline = monotonic_time.perf_counter() + timeout_seconds
        prepared: list[tuple[SearchTarget, str, dict[str, Any]]] = []
        for target in targets:
            membership_target = target == "subscriptions" and user_id is not None
            index_uid = MEMBERSHIPS_INDEX if membership_target else MEILI_TARGET_INDEX[target]
            target_limit = (
                min(limit, 10)
                if query.scope == "global" and len(targets) > 1
                else limit
            )
            search_kwargs: dict[str, Any] = {
                "offset": offset if len(targets) == 1 else 0,
                "limit": target_limit,
                "matching_strategy": _matching_strategy(target),
            }
            filter_expression = _compile_meili_filter(
                query,
                target,
                resolved,
                force_sfw=force_sfw,
                identity_field="subscription_id" if membership_target else "id",
            )
            if membership_target:
                ownership_filter = f"user_id = {_meili_literal(user_id)}"
                filter_expression = f"({filter_expression}) AND {ownership_filter}" if filter_expression else ownership_filter
            if target == "subscriptions" and not membership_target and allowed_subscription_ids is not None:
                if allowed_subscription_ids:
                    ownership_filter = "(" + " OR ".join(
                        f"id = {_meili_literal(str(subscription_id))}"
                        for subscription_id in sorted(
                            allowed_subscription_ids, key=str
                        )
                    ) + ")"
                else:
                    ownership_filter = 'id = "__no_owned_subscription__"'
                filter_expression = (
                    f"({filter_expression}) AND {ownership_filter}"
                    if filter_expression
                    else ownership_filter
                )
            if target == "repositories" and allowed_repository_ids is not None:
                if allowed_repository_ids:
                    ownership_filter = "(" + " OR ".join(
                        f"id = {_meili_literal(str(repository_id))}"
                        for repository_id in sorted(allowed_repository_ids, key=str)
                    ) + ")"
                else:
                    ownership_filter = 'id = "__no_owned_repository__"'
                filter_expression = (
                    f"({filter_expression}) AND {ownership_filter}"
                    if filter_expression
                    else ownership_filter
                )
            sort = _meili_sort(query, target)
            if membership_target and sort:
                sort = [value.replace("id:", "subscription_id:") if value.startswith("id:") else value for value in sort]
            if filter_expression:
                search_kwargs["filter"] = filter_expression
            if sort:
                search_kwargs["sort"] = sort
            if target == "works" and query.scope == "works":
                search_kwargs["attributes_to_retrieve"] = WORK_LIST_RETRIEVE_FIELDS
            prepared.append((target, index_uid, search_kwargs))

        semaphore = _meili_search_semaphore()
        acquired = False
        attempted = False
        try:
            remaining = deadline - monotonic_time.perf_counter()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(semaphore.acquire(), timeout=remaining)
            acquired = True
            _meili_breaker_before_request()
            attempted = True

            remaining = deadline - monotonic_time.perf_counter()
            if remaining <= 0:
                raise TimeoutError

            def _run_searches(socket_timeout: float = remaining) -> list[Any]:
                from types import SimpleNamespace
                requests = [(uid, kwargs) for _target, uid, kwargs in prepared]
                count_positions = {}
                for position, (_target, uid, kwargs) in enumerate(prepared):
                    if uid == MEMBERSHIPS_INDEX:
                        count_positions[position] = len(requests)
                        count_kwargs = {key: value for key, value in kwargs.items() if key not in {"offset", "limit", "sort"}}
                        count_kwargs.update(page=1, hits_per_page=1, attributes_to_retrieve=["id"])
                        requests.append((uid, count_kwargs))
                client = _client(timeout_seconds=socket_timeout)
                multi_search = getattr(client, "multi_search", None)
                if len(requests) > 1 and callable(multi_search):
                    results = list(multi_search([
                        SearchParams(index_uid=uid, query=text, **kwargs)
                        for uid, kwargs in requests
                    ]))
                else:
                    client = _client(timeout_seconds=max(0.1, socket_timeout / len(requests)))
                    results = [client.index(uid).search(text, **kwargs) for uid, kwargs in requests]
                if len(results) != len(requests):
                    raise RuntimeError("Meilisearch returned an incomplete multi-search response")
                for position, count_position in count_positions.items():
                    total = getattr(results[count_position], "total_hits", None)
                    if total is None:
                        raise RuntimeError("Meilisearch did not return an exact membership count")
                    results[position] = SimpleNamespace(hits=results[position].hits, estimated_total_hits=int(total))
                return results[:len(prepared)]

            results = await asyncio.wait_for(
                asyncio.to_thread(_run_searches),
                timeout=remaining + 0.1,
            )
            if len(results) != len(prepared):
                raise RuntimeError(
                    "Meilisearch returned an incomplete multi-search response"
                )
            _meili_breaker_success()

            output: dict[str, dict] = {}
            for (target, _index_uid, _kwargs), result in zip(prepared, results):
                hits, total = _search_hits(result)
                if target == "subscriptions" and user_id is not None:
                    # A stale document cannot re-grant a removed membership.
                    # Hydrate this page, never the actor's complete membership set.
                    ids = [UUID(str(hit["id"])) for hit in hits]
                    live = {str(member.id): member for member in (await self.db.execute(
                        select(UserSubscription).where(UserSubscription.id.in_(ids), UserSubscription.user_id == user_id)
                    )).scalars()} if ids else {}
                    safe_hits = []
                    for hit in hits:
                        member = live.get(str(hit["id"]))
                        if member is None or str(member.subscription_id) != str(hit.get("subscription_id")):
                            continue
                        hit["id"] = str(member.subscription_id)
                        hit["name"] = member.name
                        for field in ("user_id", "subscription_id", "canonical_name", "projection_hash", "projection_version"):
                            hit.pop(field, None)
                        safe_hits.append(hit)
                    total = max(0, total - (len(hits) - len(safe_hits)))
                    hits = safe_hits
                for hit in hits:
                    _decorate_alias_hit(hit, text)
                if target == "works" and query.scope == "works":
                    for hit in hits:
                        # Preserve the public document shape without loading the
                        # two largest, non-rendered fields from the hot index.
                        hit.setdefault("description", "")
                        hit.setdefault("tags", [])
                        hit.setdefault("repository_ids", [])
                        hit.setdefault("source_work_ids", [])
                output[target] = {"total": total, "items": hits}
            return output
        except SearchBackendUnavailable:
            raise
        except TimeoutError as exc:
            if attempted:
                _meili_breaker_failure()
            logger.warning("Meilisearch search timed out after %.1fs", timeout_seconds)
            raise SearchBackendUnavailable("Search index timed out") from exc
        except Exception as exc:
            if attempted:
                _meili_breaker_failure()
            logger.warning("Meilisearch search failed", exc_info=True)
            raise SearchBackendUnavailable("Search index is unavailable") from exc
        finally:
            if acquired:
                semaphore.release()

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
        resolved: dict[tuple[str, str], Any],
        offset: int,
        limit: int,
        visibility: str = "all",
        *,
        permissions: set[str] | frozenset[str],
        user_id: int | None = None,
        operation_type: str | None = None,
    ) -> dict:
        conditions = [TaskRun.kind != "account"]
        if operation_type:
            conditions.append(TaskRun.operation_type == operation_type)
        excluded_admin_operation_types = (
            inaccessible_admin_operation_types_for_permissions(permissions)
        )
        if excluded_admin_operation_types:
            conditions.append(or_(
                TaskRun.kind != "admin",
                TaskRun.operation_type.is_(None),
                TaskRun.operation_type.not_in(excluded_admin_operation_types),
            ))
        if user_id is not None:
            conditions.append(
                task_surface_visibility_condition(
                    user_id,
                    include_global_system_tasks="system" in permissions,
                )
            )
        if visibility == "actionable":
            conditions.append(
                or_(
                    TaskRun.status.in_({"enqueued", "running", "recovering", "paused"}),
                    TaskRun.attention_state == "open",
                )
            )
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
        from app.services.task_actions import enrich_actions
        await enrich_actions(self.db, rows, user_id=user_id)
        return {"total": total, "items": [task_payload(row) for row in rows]}

    async def search_tasks(
        self,
        query: str,
        *,
        visibility: str = "all",
        offset: int = 0,
        limit: int = 50,
        permissions: set[str] | frozenset[str] | None = None,
        user_id: int | None = None,
        operation_type: str | None = None,
    ) -> dict:
        parsed = parse_search_query(query, "tasks")
        resolved = await self._resolve_qualifiers(parsed)
        return await self._search_tasks(
            parsed,
            resolved,
            offset,
            limit,
            visibility=visibility,
            permissions=permissions if permissions is not None else frozenset(),
            user_id=user_id,
            operation_type=operation_type,
        )

    async def search_download_jobs(
        self,
        query: str,
        *,
        offset: int = 0,
        limit: int = 50,
        visibility: str = "all",
        user_id: int | None = None,
        subscription_id: UUID | str | None = None,
    ) -> list[DownloadJob]:
        """Return download-domain rows using the canonical task search AST.

        The task page keeps richer download controls, but its text and visual
        filters still cross the same parser/resolver seam as ``/search``.
        """

        parsed = parse_search_query(query, "tasks")
        resolved = await self._resolve_qualifiers(parsed)
        conditions = []
        if subscription_id is not None:
            conditions.append(DownloadJob.subscription_id == UUID(str(subscription_id)))
        if user_id is not None:
            from app.services.tasks import download_job_visibility_condition

            conditions.append(download_job_visibility_condition(user_id))
        if visibility == "actionable":
            conditions.append(
                or_(
                    DownloadJob.status.in_(
                        {"enqueued", "downloading", "downloaded", "importing", "recovering", "paused"}
                    ),
                    and_(
                        DownloadJob.status.in_({"failed", "stale"}),
                        exists().where(
                            TaskRun.subject_type == "download_job",
                            TaskRun.subject_id == DownloadJob.id,
                            TaskRun.attention_state == "open",
                        ),
                    ),
                )
            )
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
        visibility: str = "all",
        user_id: int | None = None,
    ) -> tuple[int, list[ImportJob]]:
        """Return import-domain rows using the canonical task search AST."""

        parsed = parse_search_query(query, "tasks")
        resolved = await self._resolve_qualifiers(parsed)
        conditions = []
        if user_id is not None:
            conditions.append(import_job_visibility_condition(user_id))
        if visibility == "actionable":
            conditions.append(
                or_(
                    ImportJob.status.in_({"enqueued", "running", "recovering", "paused"}),
                    and_(
                        ImportJob.status.in_({"failed", "stale"}),
                        exists().where(
                            TaskRun.subject_type == "import_job",
                            TaskRun.subject_id == ImportJob.id,
                            TaskRun.attention_state == "open",
                        ),
                    ),
                )
            )
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
        resolved: dict[tuple[str, str], Any],
        offset: int,
        limit: int,
        *,
        user_id: int | None = None,
    ) -> dict:
        from app.jobs.subscription_sync import schedule_decision_snapshot
        from app.services.settings import get_scheduler_config
        from app.services.subscription_calendar import effective_calendar_rule
        from app.services.auth_health import classify_source_health
        from zoneinfo import ZoneInfo

        config = await get_scheduler_config(self.db)
        tz_name = config.get("timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        now = datetime.now(tz)
        scheduler_enabled = bool(config.get("scheduler_enabled", True))
        scheduler_stmt = (
            select(SubscriptionSource, Subscription, Creator)
            .join(Subscription, SubscriptionSource.subscription_id == Subscription.id)
            .join(Creator, Subscription.creator_id == Creator.id)
        )
        if user_id is not None:
            from app.models.remote_discovery import (
                UserSubscription,
                UserSubscriptionSource,
            )

            scheduler_stmt = (
                scheduler_stmt.add_columns(UserSubscriptionSource, UserSubscription)
                .join(
                    UserSubscriptionSource,
                    UserSubscriptionSource.subscription_source_id
                    == SubscriptionSource.id,
                )
                .join(
                    UserSubscription,
                    UserSubscription.id
                    == UserSubscriptionSource.user_subscription_id,
                )
                .where(
                    UserSubscriptionSource.user_id == user_id,
                    UserSubscription.user_id == user_id,
                )
            )
        rows = (
            await self.db.execute(
                scheduler_stmt.order_by(
                    Creator.display_name,
                    Creator.name,
                    SubscriptionSource.source,
                )
            )
        ).all()
        items = []
        for row in rows:
            repository, subscription, creator = row[:3]
            source_policy = row[3] if user_id is not None else repository
            subscription_policy = row[4] if user_id is not None else subscription
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
                subscription_policy,
                config,
                source_policy.last_synced_at,
                source_policy.last_attempted_at,
                now,
                tz,
                source_policy.next_sync_at,
            )
            due = bool(decision.get("due"))
            reason = str(decision.get("reason"))
            suppression_reason = None
            auth_health = classify_source_health(source_policy, subscription_policy)
            auth_healthy = source_policy.auth_healthy is not False
            if not subscription_policy.is_active:
                due, reason = False, "subscription_inactive"
            elif not subscription_policy.sync_enabled:
                due, reason = False, "subscription_sync_disabled"
            elif not source_policy.is_enabled:
                due, reason = False, "source_disabled"
            elif auth_health.actionable:
                due, reason = False, "auth_unhealthy"
            elif not can_download:
                due, reason = False, "provider_not_downloadable"
            elif not url_valid:
                due, reason = False, "url_invalid"
            if not scheduler_enabled:
                due = False
                suppression_reason = "scheduler_disabled"
            items.append({
                "subscription_id": str(subscription.id),
                "subscription_name": subscription_policy.name,
                "subscription_active": subscription_policy.is_active,
                "subscription_sync_enabled": subscription_policy.sync_enabled,
                "creator_id": str(creator.id),
                "creator_name": creator.display_name or creator.name,
                "source_id": str(repository.id),
                "source": repository.source,
                "source_display_name": display_name,
                "source_url": repository.source_url,
                "source_creator_id": repository.source_creator_id,
                "source_enabled": source_policy.is_enabled,
                "effective_mode": decision.get("mode") or subscription_policy.schedule_mode or config.get("schedule_mode", "interval"),
                "timezone": tz_name,
                "scheduled_times": subscription_policy.scheduled_times or config.get("scheduled_times", ""),
                "schedule_rule": (
                    effective_calendar_rule(subscription_policy, config)
                    if (subscription_policy.schedule_mode or config.get("schedule_mode"))
                    in {"calendar", "fixed_time"}
                    else None
                ),
                "sync_interval_hours": subscription_policy.sync_interval_hours,
                "last_synced_at": _iso(source_policy.last_synced_at),
                "last_attempted_at": _iso(source_policy.last_attempted_at),
                "due": due,
                "decision": "due_now" if due else reason,
                "reason": reason,
                "suppression_reason": suppression_reason,
                "next_due_at": decision.get("next_due_at"),
                "window_start": decision.get("window_start"),
                "window_end": decision.get("window_end"),
                "auth_healthy": auth_healthy,
                "auth_state": auth_health.auth_state,
                "credential_state": auth_health.credential_state,
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
        allowed_repository_ids: set[UUID] | None = None,
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
            allowed_repository_ids=allowed_repository_ids,
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
        allowed_repository_ids: set[UUID] | None = None,
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
                        "description": item["description"],
                        "qualifier_key": key,
                        "help_id": item["help_id"],
                        "example": item["example"],
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
            repo_stmt = (
                select(
                    SubscriptionSource.source,
                    SubscriptionSource.source_creator_id,
                    SubscriptionSource.id,
                )
                .where(or_(
                    SubscriptionSource.source_creator_id.ilike(f"%{partial}%"),
                    SubscriptionSource.source_url.ilike(f"%{partial}%"),
                ))
            )
            if allowed_repository_ids is not None:
                repo_stmt = repo_stmt.where(
                    SubscriptionSource.id.in_(allowed_repository_ids)
                )
            rows = await self.db.execute(repo_stmt.limit(limit))
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
            select(WorkSource.work_id, Asset.id, Asset.mime_type, Asset.file_name)
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
        for work_id, asset_id, mime, file_name in asset_rows:
            work_key = str(work_id)
            rendered_asset_id = str(asset_id)
            if rendered_asset_id not in asset_id_sets[work_key]:
                asset_id_sets[work_key].add(rendered_asset_id)
                asset_ids[work_key].append(rendered_asset_id)
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

    async def index_works(self, work_ids: Iterable[UUID]) -> None:
        """Persist compatibility indexing requests without waiting on Meili.

        Domain mutation paths should use ``request_search_projection`` in the
        transaction that changes the rows.  This post-commit adapter deliberately
        does not drain the queue: a slow or unavailable search node must never
        extend an API/import transaction's critical path.
        """
        identities = tuple(dict.fromkeys(UUID(str(value)) for value in work_ids))
        if not identities:
            return
        await enqueue_projection_events(WORKS_INDEX, identities, action="upsert")

    async def drain_search_projection_outbox(self, *, limit: int = 500) -> dict[str, int | str]:
        """Run one durable submission or one-shot poll and release the worker."""
        from app.services.search_delivery import run_delivery_slice

        return await run_delivery_slice(limit=limit)

    async def _search_reference_db(
        self,
        target: str,
        offset: int,
        limit: int,
        *,
        allowed_subscription_ids: set[UUID] | None = None,
        allowed_repository_ids: set[UUID] | None = None,
        user_id: int | None = None,
    ) -> dict:
        """Direct DB list query for real-time listing — no index dependency.

        Used when the search query is empty (no text, no filters) on a single
        reference target.  Returns the same document shape as _search_meili()
        so callers don't need to distinguish the two paths.
        """
        if target == "creators":
            return await self._search_creators_db(offset, limit)
        if target == "subscriptions":
            return await self._search_subscriptions_db(
                offset,
                limit,
                allowed_subscription_ids=allowed_subscription_ids,
                user_id=user_id,
            )
        if target == "repositories" and allowed_repository_ids is not None:
            # Empty reference searches do not currently expose repositories;
            # retain that contract while keeping the ownership argument
            # explicit for future DB-backed listing support.
            return {"total": 0, "items": []}
        if target == "works":
            return await self._search_works_db(offset, limit)
        # Tags and repositories already have their own DB endpoints;
        # fall through for any future targets.
        return {"total": 0, "items": []}

    def _reference_browse_statement(
        self,
        target: SearchTarget,
        query: SearchQuery,
        resolved: dict[tuple[str, str], Any],
        *,
        allowed_subscription_ids: set[UUID] | None = None,
        user_id: int | None = None,
    ) -> tuple[Any, tuple[Any, ...], str]:
        """Build the authoritative creator/subscription browse projection."""

        if target not in {"creators", "subscriptions"}:
            raise ValueError(f"Unsupported reference browse target: {target}")

        if target == "creators":
            model = Creator
            identity = Creator.id
            name = func.coalesce(Creator.display_name, Creator.name)
            statement = select(identity.label("id"))
        else:
            model = Subscription
            identity = Subscription.id
            name = func.coalesce(Creator.display_name, Creator.name)
            statement = select(identity.label("id")).join(
                Creator,
                Creator.id == Subscription.creator_id,
            )
            if user_id is not None:
                statement = statement.join(
                    UserSubscription,
                    and_(
                        UserSubscription.subscription_id == Subscription.id,
                        UserSubscription.user_id == user_id,
                    ),
                )
            if allowed_subscription_ids is not None:
                statement = statement.where(
                    Subscription.id.in_(allowed_subscription_ids)
                )

        conditions: list[Any] = []
        for (key, negated), tokens in _grouped_qualifiers(query, target).items():
            expressions: list[Any] = []
            for token in tokens:
                value = _resolved_value(token, resolved)
                expression = None
                if key == "uid":
                    source, source_creator_id = parse_source_identity(token.value)
                    if target == "creators":
                        expression = or_(
                            Creator.id.in_(
                                select(SourceCreator.creator_id).where(
                                    SourceCreator.creator_id.is_not(None),
                                    SourceCreator.source == source,
                                    SourceCreator.source_creator_id
                                    == source_creator_id,
                                )
                            ),
                            Creator.id.in_(
                                select(Subscription.creator_id)
                                .join(
                                    SubscriptionSource,
                                    SubscriptionSource.subscription_id
                                    == Subscription.id,
                                )
                                .where(
                                    SubscriptionSource.source == source,
                                    SubscriptionSource.source_creator_id
                                    == source_creator_id,
                                )
                            ),
                        )
                    else:
                        expression = Subscription.id.in_(
                            select(SubscriptionSource.subscription_id).where(
                                SubscriptionSource.source == source,
                                SubscriptionSource.source_creator_id
                                == source_creator_id,
                            )
                        )
                elif key == "url":
                    source_url = _resolved_source_url(token, resolved)
                    identities = source_url.ids_for(target) if source_url else ()
                    expression = identity.in_(
                        [UUID(item) for item in identities]
                    )
                elif key == "source":
                    if target == "creators":
                        expression = Creator.id.in_(
                            select(SourceCreator.creator_id).where(
                                SourceCreator.creator_id.is_not(None),
                                SourceCreator.source == value,
                            )
                        )
                    else:
                        expression = Subscription.id.in_(
                            select(SubscriptionSource.subscription_id).where(
                                SubscriptionSource.source == value
                            )
                        )
                elif key == "creator":
                    creator_id = UUID(value)
                    expression = (
                        Creator.id == creator_id
                        if target == "creators"
                        else Subscription.creator_id == creator_id
                    )
                elif key == "repo" and target == "subscriptions":
                    repository_id = UUID(value)
                    expression = Subscription.id.in_(
                        select(SubscriptionSource.subscription_id).where(
                            SubscriptionSource.id == repository_id
                        )
                    )
                elif key == "is":
                    if target == "creators":
                        expression = {
                            "favorite": Creator.is_favorite.is_(True),
                            "active": Creator.is_active.is_(True),
                            "inactive": Creator.is_active.is_(False),
                        }.get(value)
                    else:
                        state_model = (
                            UserSubscription
                            if user_id is not None
                            else Subscription
                        )
                        expression = {
                            "active": state_model.is_active.is_(True),
                            "inactive": state_model.is_active.is_(False),
                            "sync-enabled": state_model.sync_enabled.is_(True),
                            "sync-disabled": state_model.sync_enabled.is_(False),
                            "never-synced": self._subscription_never_synced_expression(
                                user_id=user_id
                            ),
                        }.get(value)
                elif key == "has":
                    if target == "creators":
                        expression = {
                            "subscription": select(Subscription.id).where(
                                Subscription.creator_id == Creator.id
                            ).exists(),
                            "repository": select(SubscriptionSource.id)
                            .join(
                                Subscription,
                                Subscription.id
                                == SubscriptionSource.subscription_id,
                            )
                            .where(Subscription.creator_id == Creator.id)
                            .exists(),
                            "danbooru": Creator.danbooru_artist_id.is_not(None),
                        }.get(value)
                    elif value == "last-sync":
                        expression = self._subscription_has_last_sync_expression(
                            user_id=user_id
                        )
                elif key in {"created", "updated", "synced"}:
                    if key == "synced" and target == "subscriptions":
                        expression = or_(
                            _sql_date_expression(
                                Subscription.last_synced_at,
                                value,
                            ),
                            select(SubscriptionSource.id).where(
                                SubscriptionSource.subscription_id
                                == Subscription.id,
                                _sql_date_expression(
                                    SubscriptionSource.last_synced_at,
                                    value,
                                ),
                            ).exists(),
                        )
                    elif key != "synced":
                        expression = _sql_date_expression(
                            getattr(model, f"{key}_at"),
                            value,
                        )
                if expression is not None:
                    expressions.append(
                        not_(expression) if negated else expression
                    )
            if expressions:
                conditions.append(
                    and_(*expressions) if negated else or_(*expressions)
                )

        if conditions:
            statement = statement.where(and_(*conditions))

        normalized_name, anchor_key, anchor_rank = (
            _reference_name_expressions(name)
        )
        selected_sort = query.values("sort")
        sort_name = selected_sort[0] if selected_sort else "name-asc"
        direction = "asc" if sort_name.endswith("-asc") else "desc"
        if sort_name.startswith("name-"):
            sort_value = normalized_name
        elif sort_name.startswith("created-"):
            sort_value = model.created_at
        elif sort_name.startswith("updated-"):
            sort_value = model.updated_at
        else:
            sort_value = (
                Subscription.last_synced_at
                if target == "subscriptions"
                else model.updated_at
            )

        projection = statement.add_columns(
            normalized_name.label("name_sort"),
            anchor_key.label("anchor_key"),
            anchor_rank.label("anchor_rank"),
            sort_value.label("sort_value"),
        ).subquery()
        if sort_name.startswith("name-"):
            name_sort = projection.c.name_sort.collate("und-x-icu")
            if direction == "asc":
                order_by = (
                    projection.c.anchor_rank.asc(),
                    name_sort.asc(),
                    projection.c.id.asc(),
                )
            else:
                order_by = (
                    projection.c.anchor_rank.desc(),
                    name_sort.desc(),
                    projection.c.id.desc(),
                )
        else:
            value_order = (
                projection.c.sort_value.asc().nulls_last()
                if direction == "asc"
                else projection.c.sort_value.desc().nulls_last()
            )
            identity_order = (
                projection.c.id.asc()
                if direction == "asc"
                else projection.c.id.desc()
            )
            order_by = (value_order, identity_order)
        return projection, order_by, direction

    @staticmethod
    def _subscription_has_last_sync_expression(*, user_id: int | None) -> Any:
        if user_id is not None:
            return select(UserSubscriptionSource.id).where(
                UserSubscriptionSource.user_subscription_id
                == UserSubscription.id,
                UserSubscriptionSource.user_id == user_id,
                UserSubscriptionSource.last_synced_at.is_not(None),
            ).exists()
        return select(SubscriptionSource.id).where(
            SubscriptionSource.subscription_id == Subscription.id,
            SubscriptionSource.last_synced_at.is_not(None),
        ).exists()

    @classmethod
    def _subscription_never_synced_expression(
        cls,
        *,
        user_id: int | None,
    ) -> Any:
        canonical_missing = Subscription.last_synced_at.is_(None)
        return and_(
            canonical_missing,
            not_(
                cls._subscription_has_last_sync_expression(user_id=user_id)
            ),
        )

    async def _search_browse_reference_db(
        self,
        target: SearchTarget,
        query: SearchQuery,
        resolved: dict[tuple[str, str], Any],
        offset: int,
        limit: int,
        *,
        allowed_subscription_ids: set[UUID] | None = None,
        user_id: int | None = None,
    ) -> dict:
        projection, order_by, _direction = self._reference_browse_statement(
            target,
            query,
            resolved,
            allowed_subscription_ids=allowed_subscription_ids,
            user_id=user_id,
        )
        total = int(
            (
                await self.db.execute(
                    select(func.count()).select_from(projection)
                )
            ).scalar_one()
        )
        identities = list(
            (
                await self.db.execute(
                    select(projection.c.id)
                    .order_by(*order_by)
                    .offset(offset)
                    .limit(limit)
                )
            ).scalars()
        )
        if not identities:
            return {"total": total, "items": []}
        builder = (
            self._build_creator_documents
            if target == "creators"
            else self._build_subscription_documents
        )
        documents = await builder(identities)
        by_id = {document["id"]: document for document in documents}
        return {
            "total": total,
            "items": [
                by_id[str(identity)]
                for identity in identities
                if str(identity) in by_id
            ],
        }

    async def name_anchors(
        self,
        *,
        scope: SearchScope,
        query: str,
        permissions: set[str],
        allowed_subscription_ids: set[UUID] | None = None,
        user_id: int | None = None,
    ) -> dict:
        parsed = parse_search_query(query, scope)
        targets = self._allowed_targets(parsed, permissions)
        selected_sort = parsed.values("sort")
        if (
            scope not in {"creators", "subscriptions"}
            or targets != (scope,)
            or parsed.terms
            or (
                selected_sort
                and selected_sort[0] not in {"name-asc", "name-desc"}
            )
        ):
            raise NameAnchorsUnavailable(
                "Name anchors require a structured reference query sorted by name"
            )
        resolved = await self._resolve_qualifiers(parsed)
        projection, order_by, direction = self._reference_browse_statement(
            scope,
            parsed,
            resolved,
            allowed_subscription_ids=allowed_subscription_ids,
            user_id=user_id,
        )
        ordered = select(
            projection.c.anchor_key.label("anchor_key"),
            (
                func.row_number().over(order_by=order_by) - 1
            ).label("offset"),
        ).cte("ordered_reference_names")
        rows = (
            await self.db.execute(
                select(
                    ordered.c.anchor_key,
                    func.min(ordered.c.offset),
                    func.count(),
                ).group_by(ordered.c.anchor_key)
            )
        ).all()
        aggregates = {
            key: {"offset": int(offset), "count": int(count)}
            for key, offset, count in rows
        }
        definitions = (
            reversed(REFERENCE_NAME_ANCHORS)
            if direction == "desc"
            else REFERENCE_NAME_ANCHORS
        )
        items = []
        for definition in definitions:
            aggregate = aggregates.get(definition["key"])
            items.append(
                {
                    **definition,
                    "offset": aggregate["offset"] if aggregate else None,
                    "count": aggregate["count"] if aggregate else 0,
                }
            )
        return {
            "scope": scope,
            "direction": direction,
            "total": sum(item["count"] for item in items),
            "items": items,
        }

    async def _search_identity_reference_db(
        self,
        target: SearchTarget,
        query: SearchQuery,
        resolved: dict[tuple[str, str], Any],
        offset: int,
        limit: int,
        *,
        allowed_subscription_ids: set[UUID] | None = None,
        user_id: int | None = None,
        allowed_repository_ids: set[UUID] | None = None,
    ) -> dict:
        """Execute source-identity reference searches against committed rows."""

        model = {
            "creators": Creator,
            "repositories": SubscriptionSource,
            "subscriptions": Subscription,
        }[target]
        conditions: list[Any] = []
        if target == "subscriptions" and user_id is not None:
            conditions.append(Subscription.id.in_(select(UserSubscription.subscription_id).where(UserSubscription.user_id == user_id)))
        if target == "subscriptions" and allowed_subscription_ids is not None:
            conditions.append(Subscription.id.in_(allowed_subscription_ids))
        if target == "repositories" and allowed_repository_ids is not None:
            conditions.append(SubscriptionSource.id.in_(allowed_repository_ids))

        for (key, negated), tokens in _grouped_qualifiers(query, target).items():
            expressions: list[Any] = []
            for token in tokens:
                value = _resolved_value(token, resolved)
                expression = None
                if key == "uid":
                    source, identity = parse_source_identity(token.value)
                    if target == "creators":
                        expression = or_(
                            Creator.id.in_(
                                select(SourceCreator.creator_id).where(
                                    SourceCreator.creator_id.is_not(None),
                                    SourceCreator.source == source,
                                    SourceCreator.source_creator_id == identity,
                                )
                            ),
                            Creator.id.in_(
                                select(Subscription.creator_id)
                                .join(
                                    SubscriptionSource,
                                    SubscriptionSource.subscription_id == Subscription.id,
                                )
                                .where(
                                    SubscriptionSource.source == source,
                                    SubscriptionSource.source_creator_id == identity,
                                )
                            ),
                        )
                    elif target == "repositories":
                        expression = and_(
                            SubscriptionSource.source == source,
                            SubscriptionSource.source_creator_id == identity,
                        )
                    else:
                        expression = Subscription.id.in_(
                            select(SubscriptionSource.subscription_id).where(
                                SubscriptionSource.source == source,
                                SubscriptionSource.source_creator_id == identity,
                            )
                        )
                elif key == "url":
                    source_url = _resolved_source_url(token, resolved)
                    identities = source_url.ids_for(target) if source_url else ()
                    expression = model.id.in_([UUID(identity) for identity in identities])
                elif key == "source":
                    if target == "creators":
                        expression = Creator.id.in_(
                            select(SourceCreator.creator_id).where(
                                SourceCreator.creator_id.is_not(None),
                                SourceCreator.source == value,
                            )
                        )
                    elif target == "repositories":
                        expression = SubscriptionSource.source == value
                    else:
                        expression = Subscription.id.in_(
                            select(SubscriptionSource.subscription_id).where(
                                SubscriptionSource.source == value
                            )
                        )
                elif key == "creator":
                    creator_id = UUID(value)
                    if target == "creators":
                        expression = Creator.id == creator_id
                    elif target == "repositories":
                        expression = SubscriptionSource.subscription_id.in_(
                            select(Subscription.id).where(Subscription.creator_id == creator_id)
                        )
                    else:
                        expression = Subscription.creator_id == creator_id
                elif key == "repo":
                    repository_id = UUID(value)
                    if target == "repositories":
                        expression = SubscriptionSource.id == repository_id
                    elif target == "subscriptions":
                        expression = Subscription.id.in_(
                            select(SubscriptionSource.subscription_id).where(
                                SubscriptionSource.id == repository_id
                            )
                        )
                elif key == "is":
                    if target == "creators":
                        expression = {
                            "favorite": Creator.is_favorite.is_(True),
                            "active": Creator.is_active.is_(True),
                            "inactive": Creator.is_active.is_(False),
                        }.get(value)
                    elif target == "repositories":
                        from app.services.auth_health import (
                            auth_healthy_condition,
                            auth_unhealthy_condition,
                        )

                        expression = {
                            "enabled": SubscriptionSource.is_enabled.is_(True),
                            "disabled": SubscriptionSource.is_enabled.is_(False),
                            "auth-ok": auth_healthy_condition(SubscriptionSource),
                            "auth-error": auth_unhealthy_condition(SubscriptionSource),
                        }.get(value)
                    else:
                        expression = {
                            "active": Subscription.is_active.is_(True),
                            "inactive": Subscription.is_active.is_(False),
                            "sync-enabled": Subscription.sync_enabled.is_(True),
                            "sync-disabled": Subscription.sync_enabled.is_(False),
                            "never-synced": and_(
                                Subscription.last_synced_at.is_(None),
                                ~select(SubscriptionSource.id).where(
                                    SubscriptionSource.subscription_id == Subscription.id,
                                    SubscriptionSource.last_synced_at.is_not(None),
                                ).exists(),
                            ),
                        }.get(value)
                elif key == "has":
                    if target == "creators":
                        expression = {
                            "subscription": select(Subscription.id).where(
                                Subscription.creator_id == Creator.id
                            ).exists(),
                            "repository": select(SubscriptionSource.id)
                            .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
                            .where(Subscription.creator_id == Creator.id)
                            .exists(),
                            "danbooru": Creator.danbooru_artist_id.is_not(None),
                        }.get(value)
                    elif target == "repositories":
                        expression = {
                            "last-sync": SubscriptionSource.last_synced_at.is_not(None),
                            "source-creator-id": SubscriptionSource.source_creator_id.is_not(None),
                        }.get(value)
                    else:
                        expression = select(SubscriptionSource.id).where(
                            SubscriptionSource.subscription_id == Subscription.id,
                            SubscriptionSource.last_synced_at.is_not(None),
                        ).exists()
                elif key in {"created", "updated", "synced"}:
                    if key == "synced":
                        if target == "subscriptions":
                            expression = or_(
                                _sql_date_expression(Subscription.last_synced_at, value),
                                select(SubscriptionSource.id).where(
                                    SubscriptionSource.subscription_id == Subscription.id,
                                    _sql_date_expression(SubscriptionSource.last_synced_at, value),
                                ).exists(),
                            )
                        elif target == "repositories":
                            expression = _sql_date_expression(SubscriptionSource.last_synced_at, value)
                    else:
                        expression = _sql_date_expression(getattr(model, f"{key}_at"), value)
                if expression is not None:
                    expressions.append(not_(expression) if negated else expression)
            if expressions:
                conditions.append(and_(*expressions) if negated else or_(*expressions))

        base = select(model.id)
        if conditions:
            base = base.where(and_(*conditions))
        total = int((await self.db.execute(
            select(func.count()).select_from(base.order_by(None).subquery())
        )).scalar_one())

        selected_sort = query.values("sort")
        sort_name = selected_sort[0] if selected_sort else None
        ascending = bool(sort_name and sort_name.endswith("-asc"))
        if sort_name and sort_name.startswith("name-"):
            column = {
                "creators": Creator.name,
                "repositories": SubscriptionSource.source_creator_id,
                "subscriptions": Subscription.name,
            }[target]
        elif sort_name and sort_name.startswith("last-sync-"):
            column = (
                SubscriptionSource.last_synced_at
                if target == "repositories" else Subscription.last_synced_at
            )
        elif sort_name and sort_name.startswith("created-"):
            column = model.created_at
        else:
            column = model.updated_at if target != "creators" else Creator.name
            ascending = target == "creators" if not sort_name else ascending
        order = column.asc().nulls_last() if ascending else column.desc().nulls_last()
        ids = list((await self.db.execute(
            base.order_by(order, model.id.asc()).offset(offset).limit(limit)
        )).scalars().all())
        if not ids:
            return {"total": total, "items": []}

        builders = {
            "creators": self._build_creator_documents,
            "repositories": self._build_repository_documents,
            "subscriptions": self._build_subscription_documents,
        }
        documents = await builders[target](ids)
        documents_by_id = {document["id"]: document for document in documents}
        return {
            "total": total,
            "items": [documents_by_id[str(identity)] for identity in ids if str(identity) in documents_by_id],
        }

    async def _search_creators_db(self, offset: int, limit: int) -> dict:
        # Total count
        total = (await self.db.execute(
            select(func.count()).select_from(Creator)
        )).scalar() or 0

        # Paginated rows
        rows = (await self.db.execute(
            select(Creator).order_by(Creator.name).offset(offset).limit(limit)
        )).scalars().all()

        if not rows:
            return {"total": total, "items": []}

        creator_ids = [row.id for row in rows]

        # Source counts
        source_rows = (await self.db.execute(
            select(SourceCreator.creator_id, SourceCreator.source, SourceCreator.source_creator_id)
            .where(SourceCreator.creator_id.in_(creator_ids))
        )).all()
        sources: dict[str, set[str]] = defaultdict(set)
        source_ids: dict[str, set[str]] = defaultdict(set)
        for cid, source, sc_id in source_rows:
            sources[str(cid)].add(source)
            source_ids[str(cid)].add(sc_id)

        # Subscription / repository counts
        sub_rows = (await self.db.execute(
            select(
                Subscription.creator_id,
                func.count(func.distinct(Subscription.id)),
                func.count(SubscriptionSource.id),
                func.max(SubscriptionSource.last_synced_at),
            )
            .outerjoin(SubscriptionSource, SubscriptionSource.subscription_id == Subscription.id)
            .where(Subscription.creator_id.in_(creator_ids))
            .group_by(Subscription.creator_id)
        )).all()
        sub_counts = {
            str(cid): (int(sc), int(rc), ls)
            for cid, sc, rc, ls in sub_rows
        }

        items = [{
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
            "created_at": _iso(creator.created_at),
            "updated_at": _iso(creator.updated_at),
            "created_ts": _timestamp(creator.created_at),
            "updated_ts": _timestamp(creator.updated_at),
        } for creator in rows]

        return {"total": total, "items": items}

    async def _search_subscriptions_db(
        self,
        offset: int,
        limit: int,
        *,
        allowed_subscription_ids: set[UUID] | None = None,
        user_id: int | None = None,
    ) -> dict:
        # Total count
        ownership = (
            Subscription.id.in_(allowed_subscription_ids)
            if allowed_subscription_ids is not None
            else None
        )
        if user_id is not None:
            actor_ownership = Subscription.id.in_(select(UserSubscription.subscription_id).where(UserSubscription.user_id == user_id))
            ownership = and_(ownership, actor_ownership) if ownership is not None else actor_ownership
        count_stmt = select(func.count()).select_from(Subscription)
        if ownership is not None:
            count_stmt = count_stmt.where(ownership)
        total = (await self.db.execute(count_stmt)).scalar() or 0

        # Paginated rows
        rows_stmt = (
            select(Subscription, Creator)
            .join(Creator, Creator.id == Subscription.creator_id)
            .order_by(Subscription.updated_at.desc())
            .offset(offset).limit(limit)
        )
        if ownership is not None:
            rows_stmt = rows_stmt.where(ownership)
        rows = (await self.db.execute(rows_stmt)).all()

        if not rows:
            return {"total": total, "items": []}

        sub_ids = [sub.id for sub, _ in rows]

        from app.services.tasks import (
            download_job_visibility_condition,
            task_visibility_condition,
        )

        job_visibility = (
            download_job_visibility_condition(user_id)
            if user_id is not None
            else None
        )
        task_visibility = (
            task_visibility_condition(user_id) if user_id is not None else None
        )

        # Sources per subscription
        source_rows = (await self.db.execute(
            select(SubscriptionSource)
            .where(SubscriptionSource.subscription_id.in_(sub_ids))
            .order_by(SubscriptionSource.created_at)
        )).scalars().all()
        by_sub: dict[str, list[SubscriptionSource]] = defaultdict(list)
        for repo in source_rows:
            by_sub[str(repo.subscription_id)].append(repo)

        running_stmt = select(
            DownloadJob.subscription_id,
            func.count(DownloadJob.id),
        ).where(
            DownloadJob.subscription_id.in_(sub_ids),
            DownloadJob.status.in_(
                {"enqueued", "downloading", "downloaded", "importing"}
            ),
        )
        if job_visibility is not None:
            running_stmt = running_stmt.where(job_visibility)
        running_rows = (
            await self.db.execute(running_stmt.group_by(DownloadJob.subscription_id))
        ).all()
        running_by_sub = {
            str(subscription_id): int(count)
            for subscription_id, count in running_rows
        }
        actionable_stmt = (
            select(DownloadJob, TaskRun)
            .join(
                TaskRun,
                and_(
                    TaskRun.subject_type == "download_job",
                    TaskRun.subject_id == DownloadJob.id,
                ),
            )
            .where(
                DownloadJob.subscription_id.in_(sub_ids),
                or_(
                    TaskRun.status.in_({"enqueued", "running", "paused", "recovering"}),
                    TaskRun.attention_state == "open",
                ),
            )
        )
        if job_visibility is not None:
            actionable_stmt = actionable_stmt.where(job_visibility)
        if task_visibility is not None:
            actionable_stmt = actionable_stmt.where(task_visibility)
        actionable_rows = (
            await self.db.execute(
                actionable_stmt.order_by(TaskRun.updated_at.desc(), TaskRun.id.desc())
            )
        ).all()
        actionable_by_sub: dict[str, list[tuple[DownloadJob, TaskRun]]] = defaultdict(list)
        for job, task in actionable_rows:
            actionable_by_sub[str(job.subscription_id)].append((job, task))

        items = []
        for subscription, creator in rows:
            repositories = by_sub[str(subscription.id)]
            actionable = actionable_by_sub[str(subscription.id)]
            latest_job = actionable[0] if actionable else None
            latest_sync = max(
                (repo.last_synced_at for repo in repositories if repo.last_synced_at),
                default=subscription.last_synced_at,
            )
            items.append({
                "id": str(subscription.id),
                "name": subscription.name or creator.display_name or creator.name,
                "name_sort": _normalize_reference_name(
                    creator.display_name or creator.name
                ),
                "creator_id": str(creator.id),
                "creator_name": creator.display_name or creator.name,
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
                "running_job_count": running_by_sub.get(str(subscription.id), 0),
                "failed_job_count": sum(1 for _job, task in actionable if task.attention_state == "open"),
                "latest_job_id": str(latest_job[0].id) if latest_job else None,
                "latest_job_status": latest_job[1].status if latest_job else None,
                "latest_job_created_at": _iso(latest_job[1].updated_at) if latest_job else None,
                "sources": sorted({repo.source for repo in repositories}),
                "source_urls": [repo.source_url for repo in repositories if repo.source_url],
                "source_creator_ids": [repo.source_creator_id for repo in repositories if repo.source_creator_id],
                "created_at": _iso(subscription.created_at),
                "updated_at": _iso(subscription.updated_at),
                "created_ts": _timestamp(subscription.created_at),
                "updated_ts": _timestamp(subscription.updated_at),
                "synced_ts": _timestamp(latest_sync),
            })

        return {"total": total, "items": items}

    @staticmethod
    def _works_db_compatible(query: SearchQuery) -> bool:
        """Whether a work query can use the real-time list projection."""
        supported = {
            "type",
            "source",
            "creator",
            "repo",
            "tag",
            "is",
            "has",
            "posted",
            "created",
            "updated",
            "sort",
            "uid",
            "pid",
            "url",
        }
        visibility_values = {
            token.value for token in query.qualifiers
            if token.key == "is" and not token.negated and token.value in {"visible", "trashed"}
        }
        return len(visibility_values) <= 1 and all(
            token.key in supported
            and (
                token.key != "has"
                or token.value
                in {
                    "tags",
                    "description",
                    "multiple-assets",
                    "image",
                    "animation",
                    "video",
                }
            )
            for token in query.qualifiers
        )

    @staticmethod
    def _work_visibility_expressions():
        non_visible = select(WorkCurationState.id).where(
            WorkCurationState.work_id == Work.id,
            WorkCurationState.visibility != literal_column("'visible'"),
        ).exists()
        trashed = select(WorkCurationState.id).where(
            WorkCurationState.work_id == Work.id,
            WorkCurationState.visibility == literal_column("'trashed'"),
        ).exists()
        return not_(non_visible), trashed

    @staticmethod
    def _work_has_tags_expression():
        direct = select(WorkTag.id).where(WorkTag.work_id == Work.id).exists()
        sourced = (
            select(WorkSourceTag.id)
            .join(WorkSource, WorkSource.id == WorkSourceTag.work_source_id)
            .where(WorkSource.work_id == Work.id)
            .exists()
        )
        return or_(direct, sourced)

    def _work_filter_conditions(
        self,
        query: SearchQuery,
        resolved: dict[tuple[str, str], Any],
        *,
        force_sfw: bool,
    ) -> tuple[list[Any], set[str], Any]:
        """Compile the SQL projection with the same qualifier semantics as Meili."""

        visible_expression, trashed_expression = self._work_visibility_expressions()
        has_tags_expression = self._work_has_tags_expression()
        conditions: list[Any] = []
        requested_visibility = {
            token.value for token in query.qualifiers
            if token.key == "is" and not token.negated and token.value in {"visible", "trashed"}
        }
        conditions.append(
            trashed_expression if requested_visibility == {"trashed"} else visible_expression
        )
        if force_sfw:
            conditions.append(Work.is_nsfw.is_(False))

        multi_asset_ids = (
            select(WorkSource.work_id)
            .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
            .group_by(WorkSource.work_id)
            .having(func.count(func.distinct(AssetSource.asset_id)) > 1)
        )

        def media_exists(kind: str):
            lowered_mime = func.lower(func.coalesce(Asset.mime_type, ""))
            lowered_name = func.lower(func.coalesce(Asset.file_name, ""))
            lowered_role = func.lower(func.coalesce(AssetSource.role, ""))
            media_condition = {
                "image": lowered_mime.like("image/%"),
                "video": or_(
                    lowered_role == "video",
                    lowered_mime.like("video/%"),
                    lowered_name.like("%.mp4"),
                    lowered_name.like("%.webm"),
                ),
                "animation": or_(
                    lowered_role.in_(("animation", "archive")),
                    lowered_mime.in_(("image/gif", "image/apng")),
                    lowered_name.like("%.gif"),
                    lowered_name.like("%.zip"),
                ),
            }[kind]
            return (
                select(AssetSource.id)
                .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
                .join(Asset, Asset.id == AssetSource.asset_id)
                .where(WorkSource.work_id == Work.id, media_condition)
                .exists()
            )

        for (key, negated), tokens in _grouped_qualifiers(query, "works").items():
            expressions = []
            for token in tokens:
                value = _resolved_value(token, resolved)
                expression = None
                if key == "source":
                    expression = Work.id.in_(
                        select(WorkSource.work_id).where(WorkSource.source == value)
                    )
                elif key in {"uid", "pid"}:
                    source, identity = parse_source_identity(token.value)
                    identity_column = (
                        WorkSource.source_creator_id
                        if key == "uid" else WorkSource.source_work_id
                    )
                    expression = Work.id.in_(
                        select(WorkSource.work_id).where(
                            WorkSource.source == source,
                            identity_column == identity,
                        )
                    )
                elif key == "url":
                    source_url = _resolved_source_url(token, resolved)
                    identities = source_url.work_ids if source_url else ()
                    expression = Work.id.in_([UUID(identity) for identity in identities])
                elif key == "creator":
                    expression = Work.id.in_(
                        select(WorkSource.work_id)
                        .join(
                            SourceCreator,
                            and_(
                                SourceCreator.source == WorkSource.source,
                                SourceCreator.source_creator_id == WorkSource.source_creator_id,
                            ),
                        )
                        .where(SourceCreator.creator_id == UUID(value))
                    )
                elif key == "repo":
                    repository_id = UUID(value)
                    expression = (
                        select(WorkSource.id)
                        .join(
                            SubscriptionSource,
                            or_(
                                and_(
                                    WorkSource.source_creator_id.is_not(None),
                                    SubscriptionSource.source_creator_id.is_not(None),
                                    SubscriptionSource.source == WorkSource.source,
                                    SubscriptionSource.source_creator_id
                                    == WorkSource.source_creator_id,
                                ),
                                and_(
                                    WorkSource.source_url.is_not(None),
                                    SubscriptionSource.source_url.is_not(None),
                                    func.lower(
                                        func.rtrim(
                                            func.btrim(SubscriptionSource.source_url),
                                            "/",
                                        )
                                    )
                                    == func.lower(
                                        func.rtrim(
                                            func.btrim(WorkSource.source_url),
                                            "/",
                                        )
                                    ),
                                ),
                            ),
                        )
                        .where(
                            WorkSource.work_id == Work.id,
                            SubscriptionSource.id == repository_id,
                        )
                        .exists()
                    )
                elif key == "tag":
                    direct_tag_ids = (
                        select(WorkTag.work_id)
                        .join(Tag, Tag.id == WorkTag.tag_id)
                        .where(Tag.normalized_name == value)
                    )
                    source_tag_ids = (
                        select(WorkSource.work_id)
                        .join(WorkSourceTag, WorkSourceTag.work_source_id == WorkSource.id)
                        .join(Tag, Tag.id == WorkSourceTag.tag_id)
                        .where(Tag.normalized_name == value)
                    )
                    expression = or_(
                        Work.id.in_(direct_tag_ids),
                        Work.id.in_(source_tag_ids),
                    )
                elif key == "is":
                    expression = {
                        "favorite": Work.is_favorite.is_(True),
                        "nsfw": Work.is_nsfw.is_(True),
                        "sfw": Work.is_nsfw.is_(False),
                        "ai": Work.is_ai_generated.is_(True),
                        "human": Work.is_ai_generated.is_(False),
                        "visible": visible_expression,
                        "trashed": trashed_expression,
                    }.get(value)
                elif key == "has":
                    if value == "tags":
                        expression = has_tags_expression
                    elif value == "description":
                        expression = func.length(
                            func.btrim(func.coalesce(Work.description, ""))
                        ) > 0
                    elif value == "multiple-assets":
                        expression = Work.id.in_(multi_asset_ids)
                    elif value in {"image", "animation", "video"}:
                        expression = media_exists(value)
                elif key in {"posted", "created", "updated"}:
                    expression = _sql_date_expression(getattr(Work, f"{key}_at"), value)
                if expression is not None:
                    expressions.append(not_(expression) if negated else expression)
            if expressions:
                conditions.append(and_(*expressions) if negated else or_(*expressions))
        return conditions, requested_visibility, has_tags_expression

    async def _cached_work_total(
        self,
        base,
        query: SearchQuery,
        *,
        force_sfw: bool,
    ) -> int:
        generation = await asyncio.to_thread(cache_generation, "works")
        total_key = cache_key(
            "works:count",
            query=query.canonical,
            force_sfw=force_sfw,
            generation=generation,
        )
        redis_available, cached_total = await asyncio.to_thread(
            cache_get_with_status,
            total_key,
        )
        if isinstance(cached_total, int):
            return cached_total

        local_lock = _count_locks.get(total_key)
        if local_lock is None:
            local_lock = asyncio.Lock()
            _count_locks[total_key] = local_lock

        async with local_lock:
            redis_available, cached_total = await asyncio.to_thread(
                cache_get_with_status,
                total_key,
            )
            if isinstance(cached_total, int):
                return cached_total

            lease_name = hashlib.sha256(total_key.encode("utf-8")).hexdigest()
            lease = (
                await asyncio.to_thread(cache_try_lock, lease_name, 5)
                if redis_available
                else None
            )
            if redis_available and lease is None:
                # Another process owns the refresh.  Wait for that exact
                # generation; returning a generation-independent value can
                # expose a stale total immediately after a committed mutation.
                deadline = monotonic_time.monotonic() + 5.25
                while monotonic_time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                    redis_available, cached_total = await asyncio.to_thread(
                        cache_get_with_status,
                        total_key,
                    )
                    if isinstance(cached_total, int):
                        return cached_total
                    if not redis_available:
                        break
                if redis_available:
                    # The five-second lease should now be expired unless the
                    # winner published.  One contender takes over; the rest
                    # fail explicitly instead of all counting concurrently.
                    lease = await asyncio.to_thread(cache_try_lock, lease_name, 5)
                    if lease is None:
                        raise SearchBackendUnavailable(
                            "Exact works count refresh is already in progress"
                        )

            try:
                total = int((await self.db.execute(
                    select(func.count()).select_from(
                        base.with_only_columns(Work.id).order_by(None).subquery()
                    )
                )).scalar() or 0)
                await asyncio.to_thread(
                    cache_set, total_key, total, INTERACTIVE_LIST_TTL,
                )
                return total
            finally:
                if lease is not None:
                    await asyncio.to_thread(cache_release_lock, lease_name, lease)

    async def _search_works_db(
        self,
        query: SearchQuery,
        resolved: dict[tuple[str, str], Any],
        offset: int,
        limit: int,
        *,
        force_sfw: bool = False,
        cursor: str | None = None,
    ) -> dict:
        """Real-time PostgreSQL work list with bounded page hydration."""

        conditions, requested_visibility, has_tags_expression = self._work_filter_conditions(
            query,
            resolved,
            force_sfw=force_sfw,
        )
        base = select(Work).where(and_(*conditions))
        total = await self._cached_work_total(
            base,
            query,
            force_sfw=force_sfw,
        )

        page_base = base
        reverse_page = False
        if cursor:
            seek, boundary, boundary_id = _decode_work_cursor(
                cursor,
                query,
                force_sfw=force_sfw,
            )
            page_base = page_base.where(_work_seek_expression(
                query,
                seek=seek,
                value=boundary,
                identity=boundary_id,
            ))
            reverse_page = seek == "before"

        page_rows = (await self.db.execute(
            _apply_sql_sort(page_base, query, Work, reverse=reverse_page)
            .add_columns(has_tags_expression.label("has_tags"))
            .offset(0 if cursor else offset)
            .limit(limit)
        )).all()
        if reverse_page:
            page_rows.reverse()
        if not page_rows:
            return {
                "total": total,
                "items": [],
                "next_cursor": None,
                "previous_cursor": None,
            }

        work_rows = [row[0] for row in page_rows]
        has_tags = {row[0].id: bool(row[1]) for row in page_rows}
        work_ids = [work.id for work in work_rows]

        source_rows = (await self.db.execute(
            select(
                WorkSource.work_id,
                WorkSource.source,
                WorkSource.source_work_id,
                SubscriptionSource.id.label("repository_id"),
                SourceCreator.display_name,
                SourceCreator.creator_id,
                Creator.name,
                Creator.display_name,
            )
            .outerjoin(
                SourceCreator,
                and_(
                    SourceCreator.source_creator_id == WorkSource.source_creator_id,
                    SourceCreator.source == WorkSource.source,
                ),
            )
            .outerjoin(Creator, Creator.id == SourceCreator.creator_id)
            .outerjoin(
                SubscriptionSource,
                or_(
                    and_(
                        WorkSource.source_creator_id.is_not(None),
                        SubscriptionSource.source_creator_id.is_not(None),
                        SubscriptionSource.source == WorkSource.source,
                        SubscriptionSource.source_creator_id == WorkSource.source_creator_id,
                    ),
                    and_(
                        WorkSource.source_url.is_not(None),
                        SubscriptionSource.source_url.is_not(None),
                        func.lower(func.rtrim(func.btrim(SubscriptionSource.source_url), "/"))
                        == func.lower(func.rtrim(func.btrim(WorkSource.source_url), "/")),
                    ),
                ),
            )
            .where(WorkSource.work_id.in_(work_ids))
            .order_by(
                WorkSource.work_id,
                WorkSource.source,
                WorkSource.source_work_id,
                WorkSource.id,
                SubscriptionSource.id,
            )
        )).all()
        sources: dict[UUID, list[str]] = defaultdict(list)
        source_work_ids: dict[UUID, list[str]] = defaultdict(list)
        repository_ids: dict[UUID, list[str]] = defaultdict(list)
        creator_names: dict[UUID, list[str]] = defaultdict(list)
        creator_ids: dict[UUID, list[str]] = defaultdict(list)
        source_seen: dict[UUID, set[str]] = defaultdict(set)
        source_work_seen: dict[UUID, set[str]] = defaultdict(set)
        repository_seen: dict[UUID, set[str]] = defaultdict(set)
        creator_name_seen: dict[UUID, set[str]] = defaultdict(set)
        creator_id_seen: dict[UUID, set[str]] = defaultdict(set)
        for wid, source, source_work_id, repository_id, source_display, creator_id, creator_name, creator_display in source_rows:
            if source not in source_seen[wid]:
                source_seen[wid].add(source)
                sources[wid].append(source)
            if source_work_id not in source_work_seen[wid]:
                source_work_seen[wid].add(source_work_id)
                source_work_ids[wid].append(source_work_id)
            rendered_repository_id = str(repository_id) if repository_id else None
            if (
                rendered_repository_id
                and rendered_repository_id not in repository_seen[wid]
            ):
                repository_seen[wid].add(rendered_repository_id)
                repository_ids[wid].append(rendered_repository_id)
            rendered_name = creator_display or source_display or creator_name
            if rendered_name and rendered_name not in creator_name_seen[wid]:
                creator_name_seen[wid].add(rendered_name)
                creator_names[wid].append(rendered_name)
            rendered_id = str(creator_id) if creator_id else None
            if rendered_id and rendered_id not in creator_id_seen[wid]:
                creator_id_seen[wid].add(rendered_id)
                creator_ids[wid].append(rendered_id)

        # Collapse duplicate asset links in SQL, then return one aggregate row
        # plus at most ten preview IDs per work.  The old hydration transferred
        # every asset row to Python, so one unusually large work could make an
        # otherwise 30-item page consume unbounded memory.
        distinct_assets = (
            select(
                WorkSource.work_id.label("work_id"),
                Asset.id.label("asset_id"),
                Asset.mime_type.label("mime_type"),
                Asset.file_name.label("file_name"),
                func.min(AssetSource.ordinal).label("ordinal"),
                func.bool_or(
                    func.lower(func.coalesce(AssetSource.role, "")) == "video"
                ).label("video_role"),
                func.bool_or(
                    func.lower(func.coalesce(AssetSource.role, "")).in_(
                        ("animation", "archive")
                    )
                ).label("animation_role"),
            )
            .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
            .join(Asset, Asset.id == AssetSource.asset_id)
            .where(WorkSource.work_id.in_(work_ids))
            .group_by(
                WorkSource.work_id,
                Asset.id,
                Asset.mime_type,
                Asset.file_name,
            )
            .cte("work_page_distinct_assets")
            .prefix_with("MATERIALIZED", dialect="postgresql")
        )
        lowered_mime = func.lower(func.coalesce(distinct_assets.c.mime_type, ""))
        lowered_name = func.lower(func.coalesce(distinct_assets.c.file_name, ""))
        media_summary = (
            select(
                distinct_assets.c.work_id,
                func.count().label("asset_count"),
                func.bool_or(lowered_mime.like("image/%")).label("has_image"),
                func.bool_or(or_(
                    distinct_assets.c.video_role,
                    lowered_mime.like("video/%"),
                    lowered_name.like("%.mp4"),
                    lowered_name.like("%.webm"),
                )).label("has_video"),
                func.bool_or(or_(
                    distinct_assets.c.animation_role,
                    lowered_mime.in_(("image/gif", "image/apng")),
                    lowered_name.like("%.gif"),
                    lowered_name.like("%.zip"),
                )).label("has_animation"),
            )
            .group_by(distinct_assets.c.work_id)
            .cte("work_page_media_summary")
        )

        ranked_assets = select(
            distinct_assets.c.work_id,
            distinct_assets.c.asset_id,
            func.row_number().over(
                partition_by=distinct_assets.c.work_id,
                order_by=(
                    distinct_assets.c.ordinal.asc().nulls_last(),
                    distinct_assets.c.file_name,
                    distinct_assets.c.asset_id,
                ),
            ).label("preview_position"),
        ).cte("work_page_ranked_assets")
        media_previews = (
            select(
                ranked_assets.c.work_id,
                func.array_agg(aggregate_order_by(
                    ranked_assets.c.asset_id,
                    ranked_assets.c.preview_position,
                )).label("preview_asset_ids"),
            )
            .where(ranked_assets.c.preview_position <= 10)
            .group_by(ranked_assets.c.work_id)
            .cte("work_page_media_previews")
        )
        media_rows = (await self.db.execute(
            select(
                media_summary.c.work_id,
                media_summary.c.asset_count,
                media_summary.c.has_image,
                media_summary.c.has_video,
                media_summary.c.has_animation,
                media_previews.c.preview_asset_ids,
            )
            .outerjoin(
                media_previews,
                media_previews.c.work_id == media_summary.c.work_id,
            )
        )).all()
        asset_summary = {
            work_id: {
                "asset_count": int(asset_count),
                "has_image": bool(has_image),
                "has_video": bool(has_video),
                "has_animation": bool(has_animation),
                "preview_asset_ids": [str(asset_id) for asset_id in (preview_ids or [])],
            }
            for (
                work_id,
                asset_count,
                has_image,
                has_video,
                has_animation,
                preview_ids,
            ) in media_rows
        }

        visibility = "trashed" if requested_visibility == {"trashed"} else "visible"
        items: list[dict] = []
        for work in work_rows:
            summary = asset_summary.get(work.id, {})
            previews = list(summary.get("preview_asset_ids", []))
            work_sources = sources[work.id]
            work_creator_names = creator_names[work.id]
            work_creator_ids = creator_ids[work.id]
            asset_count = int(summary.get("asset_count", 0))
            has_animation = bool(summary.get("has_animation", False))
            items.append({
                "id": str(work.id),
                "title": work.title or "",
                "description": "",
                "posted_at": _iso(work.posted_at),
                "created_at": _iso(work.created_at),
                "updated_at": _iso(work.updated_at),
                "posted_ts": _timestamp(work.posted_at),
                "created_ts": _timestamp(work.created_at),
                "updated_ts": _timestamp(work.updated_at),
                "is_nsfw": bool(work.is_nsfw),
                "is_ai_generated": bool(work.is_ai_generated),
                "is_favorite": bool(work.is_favorite),
                "thumbnail_asset_id": (
                    str(work.thumbnail_asset_id)
                    if work.thumbnail_asset_id
                    else (previews[0] if previews else None)
                ),
                "preview_asset_ids": previews,
                "asset_count": asset_count,
                "source": work_sources[0] if work_sources else "unknown",
                "sources": work_sources,
                "creator_name": work_creator_names[0] if work_creator_names else "",
                "creator_names": work_creator_names,
                "creator_id": work_creator_ids[0] if work_creator_ids else "",
                "creator_ids": work_creator_ids,
                "tags": [],
                "has_tags": has_tags[work.id],
                "has_description": bool((work.description or "").strip()),
                "has_multiple_assets": asset_count > 1,
                "has_image": bool(summary.get("has_image", False)),
                "has_animation": has_animation,
                "has_ugoira": has_animation,
                "has_video": bool(summary.get("has_video", False)),
                "visibility": visibility,
                "curation_visibility": visibility,
                "repository_ids": repository_ids[work.id],
                "source_work_ids": source_work_ids[work.id],
            })
        return {
            "total": total,
            "items": items,
            "next_cursor": _encode_work_cursor(
                query,
                work_rows[-1],
                seek="after",
                force_sfw=force_sfw,
            ) if work_rows else None,
            "previous_cursor": _encode_work_cursor(
                query,
                work_rows[0],
                seek="before",
                force_sfw=force_sfw,
            ) if work_rows and (cursor or offset > 0) else None,
        }

    async def _rebuild_selected_indexes(
        self, live_indexes: tuple[str, ...], *,
        batch_size: int = WORK_DOCUMENT_BATCH_SIZE, resource_owner: str | None = None,
    ) -> dict[str, Any]:
        from app.services.search_rebuild import start_rebuild
        from app.services.outbox_coordinator import wake_pending_outboxes

        result = await start_rebuild(live_indexes, batch_size=batch_size, owner=resource_owner)
        if result["status"] == "pending":
            wake_pending_outboxes({"search": 1})
        return result

    async def refresh_reference_indexes(self) -> None:
        """Atomically refresh reference projections with bounded peak memory."""

        try:
            await self._rebuild_selected_indexes((
                CREATORS_INDEX,
                TAGS_INDEX,
                REPOSITORIES_INDEX,
                SUBSCRIPTIONS_INDEX,
                MEMBERSHIPS_INDEX,
            ))
        except Exception:
            logger.warning("Reference search indexing failed", exc_info=True)

    async def refresh_works_index(self, batch_size: int = 500) -> dict:
        """Atomically rebuild works using UUID keyset pagination."""

        try:
            result = await self._rebuild_selected_indexes(
                (WORKS_INDEX,),
                batch_size=batch_size,
            )
            return {
                **result,
                "total": int(result["counts"].get(WORKS_INDEX, 0)),
                "batches": int(result["batches"]),
                "replayed": int(result["replayed"]),
                "seconds": result["seconds"],
            }
        except Exception as exc:
            logger.exception("Works index rebuild failed")
            return {
                "status": "error",
                "message": str(exc),
                "total": 0,
                "batches": 0,
                "replayed": 0,
                "seconds": 0,
            }

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
        await enqueue_projection_events(WORKS_INDEX, [work_id], action="delete")

    async def delete_creator(self, creator_id: str) -> None:
        await enqueue_projection_events(
            CREATORS_INDEX,
            [creator_id],
            action="delete",
        )

    async def delete_tag(self, tag_id: str) -> None:
        await enqueue_projection_events(TAGS_INDEX, [tag_id], action="delete")

    async def delete_repository(self, repository_id: str) -> None:
        await enqueue_projection_events(
            REPOSITORIES_INDEX,
            [repository_id],
            action="delete",
        )

    async def delete_subscription(self, subscription_id: str) -> None:
        await enqueue_projection_events(
            SUBSCRIPTIONS_INDEX,
            [subscription_id],
            action="delete",
        )

    async def delete_all_works(self) -> None:
        try:
            await asyncio.to_thread(_delete_all_documents, WORKS_INDEX)
        except Exception:
            logger.warning("Failed to clear works search index", exc_info=True)

    @staticmethod
    async def _committed_repository_maps() -> tuple[
        dict[tuple[str, str], list[str]],
        dict[str, list[str]],
    ]:
        """Load repository lookup data without extending the audit snapshot."""

        async with async_session() as db:
            return await SearchService(db)._repository_lookup()

    @staticmethod
    async def _committed_work_documents(
        work_ids: Iterable[UUID],
        repository_maps: tuple[
            dict[tuple[str, str], list[str]],
            dict[str, list[str]],
        ],
    ) -> list[dict]:
        """Build one bounded audit batch from newly committed database state."""

        async with async_session() as db:
            service = SearchService(
                db,
                parallel_hydration=_parallel_work_hydration_supported(),
            )
            service._repository_maps = repository_maps
            return await service._build_work_documents(work_ids)

    async def audit_projection(self) -> dict[str, Any]:
        """Stream identity sets and every work hash without giant IN lists.

        Identity drift is compared as a complete set.  Work payload drift is
        then checked in bounded batches so a stale document outside the first
        UUID page cannot make the audit report a false success, while peak
        projection memory remains proportional to one batch.
        """

        models = {
            WORKS_INDEX: Work,
            CREATORS_INDEX: Creator,
            TAGS_INDEX: Tag,
            REPOSITORIES_INDEX: SubscriptionSource,
            SUBSCRIPTIONS_INDEX: Subscription,
            MEMBERSHIPS_INDEX: UserSubscription,
        }

        async def _database_ids(model: Any) -> set[str]:
            identities: set[str] = set()
            last_id: UUID | None = None
            while True:
                async with async_session() as db:
                    statement = select(model.id).order_by(model.id).limit(1000)
                    if last_id is not None:
                        statement = statement.where(model.id > last_id)
                    rows = tuple((await db.execute(statement)).scalars().all())
                if not rows:
                    break
                identities.update(str(row_id) for row_id in rows)
                last_id = rows[-1]
            return identities

        def _page_results(page: Any) -> tuple[list[dict[str, Any]], int]:
            if isinstance(page, dict):
                return list(page.get("results") or []), int(page.get("total") or 0)
            return (
                list(getattr(page, "results", []) or []),
                int(getattr(page, "total", 0) or 0),
            )

        def _read_ids(index_name: str) -> tuple[set[str], int]:
            client = _client(timeout_seconds=MEILI_WRITE_TIMEOUT_SECONDS)
            identities: set[str] = set()
            offset = 0
            total = 0
            while True:
                page = client.index(index_name).get_documents(
                    offset=offset,
                    limit=1000,
                    fields=["id"],
                )
                documents, total = _page_results(page)
                identities.update(
                    str(document["id"])
                    for document in documents
                    if document.get("id") is not None
                )
                offset += len(documents)
                if not documents or offset >= total:
                    break
            return identities, total

        # Count/identity drift is exact.  Payloads are bounded so an empty or
        # corrupted index cannot make the health response itself consume tens
        # of megabytes.
        drift_id_limit = 1000
        indexes: dict[str, dict[str, Any]] = {}
        has_drift = False
        work_ids: tuple[str, ...] = ()
        for index_name, model in models.items():
            expected = await _database_ids(model)
            if index_name == WORKS_INDEX:
                work_ids = tuple(sorted(expected))
            try:
                actual, indexed_total = await asyncio.to_thread(_read_ids, index_name)
            except Exception as exc:
                raise SearchBackendUnavailable("Search index audit is unavailable") from exc
            stale_set = actual - expected
            missing_set = expected - actual
            drifted = bool(stale_set or missing_set or indexed_total != len(actual))
            has_drift = has_drift or drifted
            indexes[INDEX_LABELS[index_name]] = {
                "database_count": len(expected),
                "index_count": indexed_total,
                "stale_count": len(stale_set),
                "missing_count": len(missing_set),
                "stale_ids": sorted(stale_set)[:drift_id_limit],
                "missing_ids": sorted(missing_set)[:drift_id_limit],
                "drift_ids_truncated": (
                    len(stale_set) > drift_id_limit
                    or len(missing_set) > drift_id_limit
                ),
                "status": "drift" if drifted else "ok",
            }

        # Identity equality alone cannot detect stale fields.  Compare every
        # deterministic projection hash, but release rich documents after each
        # bounded batch instead of retaining the full 67k-work projection.
        field_drift_ids: list[str] = []
        field_drift_count = 0
        field_audit_count = 0
        if work_ids:
            repository_maps = await self._committed_repository_maps()

            def _read_hashes(batch_ids: list[str]) -> dict[str, tuple[Any, Any]]:
                client = _client(timeout_seconds=MEILI_WRITE_TIMEOUT_SECONDS)
                page = client.index(WORKS_INDEX).get_documents(
                    filter=_meili_document_ids_filter(batch_ids),
                    limit=len(batch_ids),
                    fields=["id", "projection_version", "projection_hash"],
                )
                documents, _total = _page_results(page)
                return {
                    str(document["id"]): (
                        document.get("projection_version"),
                        document.get("projection_hash"),
                    )
                    for document in documents
                }

            for start in range(0, len(work_ids), WORK_DOCUMENT_BATCH_SIZE):
                batch_ids = list(work_ids[start:start + WORK_DOCUMENT_BATCH_SIZE])
                expected_documents = await self._committed_work_documents(
                    [UUID(value) for value in batch_ids],
                    repository_maps,
                )
                expected_hashes = {
                    str(document["id"]): (
                        document.get("projection_version"),
                        document.get("projection_hash"),
                    )
                    for document in expected_documents
                }
                try:
                    actual_hashes = await asyncio.to_thread(_read_hashes, batch_ids)
                except Exception as exc:
                    raise SearchBackendUnavailable(
                        "Search field audit is unavailable"
                    ) from exc

                batch_drift_ids = [
                    work_id
                    for work_id in batch_ids
                    if actual_hashes.get(work_id) != expected_hashes.get(work_id)
                ]
                field_audit_count += len(batch_ids)
                field_drift_count += len(batch_drift_ids)
                if len(field_drift_ids) < drift_id_limit:
                    field_drift_ids.extend(
                        batch_drift_ids[:drift_id_limit - len(field_drift_ids)]
                    )
                del expected_documents, expected_hashes, actual_hashes, batch_drift_ids

            if field_drift_count:
                has_drift = True
                indexes["works"]["status"] = "drift"

        # Keep the old field for API compatibility, but it now reports the
        # exact number of documents audited rather than an illustrative sample.
        indexes["works"]["field_sample_size"] = field_audit_count
        indexes["works"]["field_audit_count"] = field_audit_count
        indexes["works"]["field_drift_count"] = field_drift_count
        indexes["works"]["field_drift_ids"] = field_drift_ids
        indexes["works"]["field_drift_ids_truncated"] = (
            field_drift_count > len(field_drift_ids)
        )
        member_audited = 0
        member_drift = []
        member_drift_count = 0
        cursor = None
        while True:
            statement = select(UserSubscription.id).order_by(UserSubscription.id).limit(500)
            if cursor is not None:
                statement = statement.where(UserSubscription.id > cursor)
            async with async_session() as db:
                ids = tuple((await db.execute(statement)).scalars())
                if not ids:
                    break
                expected_docs = await SearchService(db)._build_membership_documents(ids)
            def read_member_hashes():
                page = _client(timeout_seconds=MEILI_WRITE_TIMEOUT_SECONDS).index(MEMBERSHIPS_INDEX).get_documents(
                    filter=_meili_document_ids_filter(map(str, ids)), limit=len(ids),
                    fields=["id", "projection_hash"],
                )
                docs, _ = _page_results(page)
                return {doc["id"]: doc.get("projection_hash") for doc in docs}
            actual = await asyncio.to_thread(read_member_hashes)
            drift = [doc["id"] for doc in expected_docs if actual.get(doc["id"]) != doc["projection_hash"]]
            member_audited += len(expected_docs)
            member_drift_count += len(drift)
            member_drift.extend(drift[:max(0, drift_id_limit - len(member_drift))])
            cursor = ids[-1]
        indexes["subscription_memberships"].update(
            field_audit_count=member_audited, field_drift_count=member_drift_count,
            field_drift_ids=member_drift, field_drift_ids_truncated=member_drift_count > len(member_drift),
        )
        if member_drift_count:
            has_drift = True
            indexes["subscription_memberships"]["status"] = "drift"
        return {"status": "drift" if has_drift else "ok", "indexes": indexes}

    async def reindex(self, *, resource_owner: str | None = None) -> dict:
        try:
            result = await self._rebuild_selected_indexes((
                WORKS_INDEX,
                CREATORS_INDEX,
                TAGS_INDEX,
                REPOSITORIES_INDEX,
                SUBSCRIPTIONS_INDEX,
                MEMBERSHIPS_INDEX,
            ), resource_owner=resource_owner)
        except Exception as exc:
            logger.exception("Search reindex failed")
            return {
                "status": "error",
                "message": str(exc),
            }
        if result["status"] != "ok":
            return result
        stats = result["counts"]
        return {
            "status": "ok",
            "message": ", ".join(
                f"{INDEX_LABELS[name]}={count}" for name, count in stats.items()
            ),
            "batches": result["batches"],
            "replayed": result["replayed"],
            "seconds": result["seconds"],
            **{INDEX_LABELS[name]: count for name, count in stats.items()},
        }
