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
from dataclasses import dataclass
from datetime import datetime, time, timezone
import hashlib
import json
import logging
import math
import struct
import tempfile
import time as monotonic_time
from pathlib import PurePosixPath
from threading import Lock
from typing import Any, Awaitable, BinaryIO, Callable, Iterable
from urllib.parse import parse_qs, unquote, urlparse
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary, WeakValueDictionary

from meilisearch_python_sdk import Client as MeiliClient
from meilisearch_python_sdk.models.search import SearchParams
from meilisearch_python_sdk.models.settings import MeilisearchSettings
from sqlalchemy import String, and_, cast, exists, func, literal_column, not_, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.models import (
    Asset,
    AssetSource,
    Creator,
    DownloadJob,
    ImportJob,
    SourceCreator,
    SearchIndexState,
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
    claim_projection_events,
    complete_projection_events,
    complete_projection_versions,
    enqueue_projection_events,
    prune_completed_projection_events,
    release_projection_events,
    replay_projection_events,
    retry_projection_events,
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
from app.services.source_search_identity import (
    ParsedSourceURL,
    parse_source_identity,
    parse_source_url,
)
from app.services.tasks import task_payload

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


async def _run_profiled_search_slice(
    owner: str,
    action: Callable[[Any], Awaitable[Any]],
    *,
    workload: str = "search_index",
    max_work_units: int = 500,
    max_slice_seconds: float = 20.0,
) -> Any:
    """Run one rebuild slice under an adaptive permit, then yield the disk.

    Full rebuilds are long-lived administrative jobs, so an RQ-level lease
    would recreate the old global lock for all 67k documents.  Each call waits
    before opening a database session, holds the profile only for one bounded
    DB/Meili batch, and performs enforce-mode duty-cycle sleep after every
    Redis/POSIX lease has been released.
    """

    from app.services.heavy_io import (
        HeavyIOUnavailable,
        heavy_io_slot,
        set_resource_state,
        wait_for_resource_capacity,
    )
    from app.services.resource_pressure import (
        profile_slice_limits,
        sleep_for_profile_slice_cooldown,
    )

    attempt = 0
    while True:
        snapshot = await wait_for_resource_capacity(
            workload=workload,
            owner=owner,
        )
        limits = profile_slice_limits(
            snapshot,
            workload,
            max_work_units=max_work_units,
            max_slice_seconds=max_slice_seconds,
        )
        if not limits.allowed:
            # The snapshot may have changed between the wait and pure limit
            # calculation.  Re-enter the event-driven hard gate without
            # opening a transaction or taking a local flock.
            continue

        entered = False
        started = monotonic_time.perf_counter()
        try:
            async with heavy_io_slot(workload, owner):
                entered = True
                result = await action(limits)
        except HeavyIOUnavailable:
            if entered:
                raise
            await asyncio.sleep(min(30.0, 2.0 ** min(attempt + 1, 5)))
            attempt += 1
            continue
        except BaseException:
            if entered:
                await set_resource_state(
                    owner,
                    "yielded",
                    "slice_failed",
                    workload=workload,
                )
                await sleep_for_profile_slice_cooldown(
                    snapshot,
                    elapsed_seconds=(
                        monotonic_time.perf_counter() - started
                    ),
                    max_seconds=300.0,
                    workload=workload,
                )
            raise

        await set_resource_state(
            owner,
            "yielded",
            "slice_complete",
            workload=workload,
        )
        await sleep_for_profile_slice_cooldown(
            snapshot,
            elapsed_seconds=monotonic_time.perf_counter() - started,
            max_seconds=300.0,
            workload=workload,
        )
        return result


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
            "source_creator_keys",
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
            "source_creator_keys",
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
            "source_creator_keys",
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

WORK_PROJECTION_VERSION = 3
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
MEILI_INCREMENTAL_TASK_TIMEOUT_MS = 30_000
MEILI_INCREMENTAL_SLICE_TIMEOUT_MS = 45_000
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
    # timestamps collide.  Explicit NULL placement also prevents the SQL and
    # indexed paths from changing page boundaries between directions.
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
                        f"id = {_meili_literal(identity)}" for identity in identities
                    )
                    expression = f"({identity_expression})"
                else:
                    expression = 'id = "__source_url_no_match__"'
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
        if target == "works" and field != "id":
            order.append(f"id:{direction}")
        return order
    if query.terms or selected == ("relevance",):
        return None
    default = DEFAULT_SORT.get(target)
    if not default:
        return None
    order = [default]
    if target == "works":
        direction = default.rsplit(":", 1)[-1]
        order.append(f"id:{direction}")
    return order


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
        parsed = parse_search_query(query, scope)
        parsed_at = monotonic_time.perf_counter()
        permission_set = permissions or {"library", "subscriptions", "tasks", "curation"}
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

        # Works list queries with no free text use PostgreSQL.  This keeps the
        # high-traffic gallery independent from the search index while still
        # preserving the public compound-search contract.
        text = _free_text(parsed)
        has_filters = bool(parsed.qualifiers)
        has_source_identity = any(
            token.key in {"uid", "pid", "url"}
            for token in parsed.qualifiers
        )
        if not text and has_source_identity:
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
                groups[target] = await self._search_reference_db(target, offset, limit)

        meili_targets = [t for t in targets if t in MEILI_TARGET_INDEX and t not in groups]
        if cursor and meili_targets:
            raise ValueError("Cursor pagination is only available for structured work lists")
        if meili_targets:
            groups.update(await self._search_meili(parsed, meili_targets, resolved, offset, limit, force_sfw))
            execution.update({
                "winner": "meilisearch",
                "consistency": "fulltext_index_required",
                "index_status": "required",
            })
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
    ) -> dict[str, dict]:
        text = _free_text(query)
        timeout_seconds = max(0.1, float(settings.meili_search_timeout_seconds))
        deadline = monotonic_time.perf_counter() + timeout_seconds
        prepared: list[tuple[SearchTarget, str, dict[str, Any]]] = []
        for target in targets:
            index_uid = MEILI_TARGET_INDEX[target]
            target_limit = (
                min(limit, 10)
                if query.scope == "global" and len(targets) > 1
                else limit
            )
            search_kwargs: dict[str, Any] = {
                "offset": offset if len(targets) == 1 else 0,
                "limit": target_limit,
            }
            filter_expression = _compile_meili_filter(
                query,
                target,
                resolved,
                force_sfw=force_sfw,
            )
            sort = _meili_sort(query, target)
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
                client = _client(timeout_seconds=socket_timeout)
                multi_search = getattr(client, "multi_search", None)
                if len(prepared) > 1 and callable(multi_search):
                    return list(multi_search([
                        SearchParams(
                            index_uid=index_uid,
                            query=text,
                            **search_kwargs,
                        )
                        for _target, index_uid, search_kwargs in prepared
                    ]))

                # Compatibility fallback for older SDKs.  Divide the socket
                # budget across targets so a cancelled executor thread cannot
                # continue issuing N full-timeout requests in the background.
                per_target_timeout = max(0.1, socket_timeout / len(prepared))
                fallback_client = _client(timeout_seconds=per_target_timeout)
                return [
                    fallback_client.index(index_uid).search(text, **search_kwargs)
                    for _target, index_uid, search_kwargs in prepared
                ]

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
    ) -> dict:
        conditions = [TaskRun.kind != "account"]
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
        return {"total": total, "items": [task_payload(row) for row in rows]}

    async def search_tasks(
        self,
        query: str,
        *,
        visibility: str = "all",
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        parsed = parse_search_query(query, "tasks")
        resolved = await self._resolve_qualifiers(parsed)
        return await self._search_tasks(
            parsed,
            resolved,
            offset,
            limit,
            visibility=visibility,
        )

    async def search_download_jobs(
        self,
        query: str,
        *,
        offset: int = 0,
        limit: int = 50,
        visibility: str = "all",
    ) -> list[DownloadJob]:
        """Return download-domain rows using the canonical task search AST.

        The task page keeps richer download controls, but its text and visual
        filters still cross the same parser/resolver seam as ``/search``.
        """

        parsed = parse_search_query(query, "tasks")
        resolved = await self._resolve_qualifiers(parsed)
        conditions = []
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
    ) -> tuple[int, list[ImportJob]]:
        """Return import-domain rows using the canonical task search AST."""

        parsed = parse_search_query(query, "tasks")
        resolved = await self._resolve_qualifiers(parsed)
        conditions = []
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
            documents.append(_with_projection_hash({
                "id": key,
                "title": work.title or "",
                "description": (work.description or "")[:1000],
                "creator_name": ordered_creator_names[0] if ordered_creator_names else "",
                "creator_names": ordered_creator_names,
                "creator_id": ordered_creator_ids[0] if ordered_creator_ids else "",
                "creator_ids": ordered_creator_ids,
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
            "source_creator_keys": sorted(source_creator_keys[str(creator.id)]),
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
        job_stats_rows = (await self.db.execute(
            select(
                DownloadJob.subscription_id,
                func.count(DownloadJob.id).filter(
                    DownloadJob.status.in_(running_statuses)
                ),
            )
            .where(DownloadJob.subscription_id.in_(selected_ids))
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
            .where(DownloadJob.subscription_id.in_(selected_ids))
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

    async def _direct_index_work_ids(
        self,
        work_ids: Iterable[UUID],
        *,
        index_uid: str = WORKS_INDEX,
    ) -> int:
        identities = tuple(dict.fromkeys(UUID(str(value)) for value in work_ids))
        if not identities:
            return 0

        def _prepare() -> MeiliClient:
            client = _client(timeout_seconds=MEILI_WRITE_TIMEOUT_SECONDS)
            _ensure_indexes(client, {index_uid: INDEX_SETTINGS[WORKS_INDEX]})
            return client

        client = await asyncio.to_thread(_prepare)
        indexed = 0
        for start in range(0, len(identities), WORK_DOCUMENT_BATCH_SIZE):
            batch_ids = identities[start:start + WORK_DOCUMENT_BATCH_SIZE]
            documents = await self._build_work_documents(batch_ids)
            present = {str(document["id"]) for document in documents}
            missing = [str(work_id) for work_id in batch_ids if str(work_id) not in present]

            def _deliver() -> None:
                _write_document_batch(client, index_uid, documents)
                _delete_document_batch(client, index_uid, missing)

            await asyncio.to_thread(_deliver)
            indexed += len(documents)
        return indexed

    async def drain_search_projection_outbox(self, *, limit: int = 500) -> dict[str, int | str]:
        """Deliver one coalesced outbox batch with one writer globally active."""

        lease = await asyncio.to_thread(
            cache_try_lock,
            INDEX_WRITE_LOCK,
            INDEX_WRITE_LOCK_TTL_SECONDS,
        )
        if lease is None:
            return {"status": "busy", "claimed": 0, "upserted": 0, "deleted": 0}

        claimed: list[ProjectionEvent] = []
        deferred: list[ProjectionEvent] = []
        try:
            claimed = await claim_projection_events(
                limit=max(1, min(int(limit), WORK_DOCUMENT_BATCH_SIZE)),
                lease_seconds=INDEX_WRITE_LOCK_TTL_SECONDS,
            )
            if not claimed:
                return {"status": "idle", "claimed": 0, "upserted": 0, "deleted": 0}

            supported_indexes = {
                WORKS_INDEX,
                CREATORS_INDEX,
                TAGS_INDEX,
                REPOSITORIES_INDEX,
                SUBSCRIPTIONS_INDEX,
            }
            unsupported = [
                event for event in claimed if event.index_uid not in supported_indexes
            ]
            if unsupported:
                raise ValueError(
                    "Unsupported search outbox indexes: "
                    + ", ".join(sorted({event.index_uid for event in unsupported}))
                )

            events_by_index: dict[str, list[ProjectionEvent]] = defaultdict(list)
            for event in claimed:
                events_by_index[event.index_uid].append(event)
            documents_by_index: dict[str, list[dict[str, Any]]] = {}
            delete_ids_by_index: dict[str, list[str]] = {}
            builder_names = {
                CREATORS_INDEX: "_build_creator_documents",
                TAGS_INDEX: "_build_tag_documents",
                REPOSITORIES_INDEX: "_build_repository_documents",
                SUBSCRIPTIONS_INDEX: "_build_subscription_documents",
            }
            # Assemble authoritative documents in a short independent read
            # transaction and close it before any network wait.
            async with async_session() as projection_db:
                projection_service = SearchService(projection_db)
                for index_uid, events in events_by_index.items():
                    upsert_events = [event for event in events if event.action == "upsert"]
                    upsert_ids = [UUID(event.entity_id) for event in upsert_events]
                    if index_uid == WORKS_INDEX:
                        documents = await projection_service._build_work_documents(upsert_ids)
                    else:
                        documents = await getattr(
                            projection_service,
                            builder_names[index_uid],
                        )(upsert_ids)
                    documents_by_index[index_uid] = documents
                    document_ids = {str(document["id"]) for document in documents}
                    # A row deleted after an upsert was queued must remove the
                    # projection instead of being acknowledged as a no-op.
                    delete_ids_by_index[index_uid] = [
                        event.entity_id
                        for event in events
                        if event.action == "delete"
                        or (
                            event.action == "upsert"
                            and event.entity_id not in document_ids
                        )
                    ]

            selected, deferred = _select_projection_event_slice(
                claimed,
                documents_by_index,
            )
            claimed = selected
            if deferred:
                await release_projection_events(deferred)

            selected_by_index: dict[str, list[ProjectionEvent]] = defaultdict(list)
            for event in claimed:
                selected_by_index[event.index_uid].append(event)
            for index_uid, index_documents in tuple(documents_by_index.items()):
                selected_upserts = {
                    event.entity_id
                    for event in selected_by_index.get(index_uid, ())
                    if event.action == "upsert"
                }
                documents_by_index[index_uid] = [
                    document
                    for document in index_documents
                    if str(document["id"]) in selected_upserts
                ]
                selected_ids = {
                    event.entity_id
                    for event in selected_by_index.get(index_uid, ())
                }
                delete_ids_by_index[index_uid] = [
                    identity
                    for identity in delete_ids_by_index[index_uid]
                    if identity in selected_ids
                ]
            events_by_index = selected_by_index

            renewed = await asyncio.to_thread(
                cache_refresh_lock,
                INDEX_WRITE_LOCK,
                lease,
                INDEX_WRITE_LOCK_TTL_SECONDS,
            )
            if not renewed:
                raise RuntimeError("Lost search index writer lease before delivery")

            def _deliver() -> None:
                client = _client(timeout_seconds=MEILI_WRITE_TIMEOUT_SECONDS)
                slice_deadline = (
                    monotonic_time.monotonic()
                    + (MEILI_INCREMENTAL_SLICE_TIMEOUT_MS / 1000)
                )

                def _remaining_task_timeout() -> int:
                    remaining_ms = int(
                        (slice_deadline - monotonic_time.monotonic()) * 1000
                    )
                    if remaining_ms <= 0:
                        raise TimeoutError("Incremental search slice timed out")
                    return max(
                        1,
                        min(MEILI_INCREMENTAL_TASK_TIMEOUT_MS, remaining_ms),
                    )

                for index_uid in INDEX_SETTINGS:
                    if index_uid not in events_by_index:
                        continue
                    _ensure_indexes(
                        client,
                        {index_uid: INDEX_SETTINGS[index_uid]},
                        task_timeout_in_ms=_remaining_task_timeout(),
                    )
                    _write_document_batch(
                        client,
                        index_uid,
                        documents_by_index[index_uid],
                        timeout_in_ms=_remaining_task_timeout(),
                    )
                    _delete_document_batch(
                        client,
                        index_uid,
                        delete_ids_by_index[index_uid],
                        timeout_in_ms=_remaining_task_timeout(),
                    )

            await asyncio.to_thread(_deliver)
            await complete_projection_events(claimed)
            await _refresh_search_index_checkpoints(events_by_index.keys())
            slice_exhausted = bool(deferred) or len(claimed) >= max(
                1,
                min(int(limit), WORK_DOCUMENT_BATCH_SIZE),
            )
            return {
                "status": "partial" if deferred else "ok",
                "claimed": len(claimed),
                "upserted": sum(len(value) for value in documents_by_index.values()),
                "deleted": sum(
                    len(set(value)) for value in delete_ids_by_index.values()
                ),
                "deferred": len(deferred),
                "slice_exhausted": slice_exhausted,
                "more_likely": slice_exhausted,
            }
        except Exception as exc:
            if claimed:
                _forget_ensured_settings(event.index_uid for event in claimed)
                await retry_projection_events(claimed, str(exc))
            if deferred:
                try:
                    await release_projection_events(deferred)
                except Exception:
                    logger.debug(
                        "Unable to release deferred search projection claims",
                        exc_info=True,
                    )
            logger.warning("Search projection outbox drain failed", exc_info=True)
            return {
                "status": "error",
                "claimed": len(claimed),
                "upserted": 0,
                "deleted": 0,
                "deferred": len(deferred),
            }
        finally:
            await asyncio.to_thread(cache_release_lock, INDEX_WRITE_LOCK, lease)

    async def _search_reference_db(
        self, target: str, offset: int, limit: int
    ) -> dict:
        """Direct DB list query for real-time listing — no index dependency.

        Used when the search query is empty (no text, no filters) on a single
        reference target.  Returns the same document shape as _search_meili()
        so callers don't need to distinguish the two paths.
        """
        if target == "creators":
            return await self._search_creators_db(offset, limit)
        if target == "subscriptions":
            return await self._search_subscriptions_db(offset, limit)
        if target == "works":
            return await self._search_works_db(offset, limit)
        # Tags and repositories already have their own DB endpoints;
        # fall through for any future targets.
        return {"total": 0, "items": []}

    async def _search_identity_reference_db(
        self,
        target: SearchTarget,
        query: SearchQuery,
        resolved: dict[tuple[str, str], Any],
        offset: int,
        limit: int,
    ) -> dict:
        """Execute source-identity reference searches against committed rows."""

        model = {
            "creators": Creator,
            "repositories": SubscriptionSource,
            "subscriptions": Subscription,
        }[target]
        conditions: list[Any] = []

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
                        expression = {
                            "enabled": SubscriptionSource.is_enabled.is_(True),
                            "disabled": SubscriptionSource.is_enabled.is_(False),
                            "auth-ok": SubscriptionSource.auth_healthy.is_not(False),
                            "auth-error": SubscriptionSource.auth_healthy.is_(False),
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

        return {"total": total, "items": items}

    async def _search_subscriptions_db(self, offset: int, limit: int) -> dict:
        # Total count
        total = (await self.db.execute(
            select(func.count()).select_from(Subscription)
        )).scalar() or 0

        # Paginated rows
        rows = (await self.db.execute(
            select(Subscription, Creator)
            .join(Creator, Creator.id == Subscription.creator_id)
            .order_by(Subscription.updated_at.desc())
            .offset(offset).limit(limit)
        )).all()

        if not rows:
            return {"total": total, "items": []}

        sub_ids = [sub.id for sub, _ in rows]

        # Sources per subscription
        source_rows = (await self.db.execute(
            select(SubscriptionSource)
            .where(SubscriptionSource.subscription_id.in_(sub_ids))
            .order_by(SubscriptionSource.created_at)
        )).scalars().all()
        by_sub: dict[str, list[SubscriptionSource]] = defaultdict(list)
        for repo in source_rows:
            by_sub[str(repo.subscription_id)].append(repo)

        running_rows = (await self.db.execute(
            select(DownloadJob.subscription_id, func.count(DownloadJob.id))
            .where(
                DownloadJob.subscription_id.in_(sub_ids),
                DownloadJob.status.in_({"enqueued", "downloading", "downloaded", "importing"}),
            )
            .group_by(DownloadJob.subscription_id)
        )).all()
        running_by_sub = {
            str(subscription_id): int(count)
            for subscription_id, count in running_rows
        }
        actionable_rows = (await self.db.execute(
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
            .order_by(TaskRun.updated_at.desc(), TaskRun.id.desc())
        )).all()
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

    @staticmethod
    async def _renew_index_write_lease(lease: str, ttl_seconds: int) -> None:
        renewed = await asyncio.to_thread(
            cache_refresh_lock,
            INDEX_WRITE_LOCK,
            lease,
            ttl_seconds,
        )
        if not renewed:
            raise RuntimeError("Lost search index writer lease during rebuild")

    @staticmethod
    async def _committed_repository_maps() -> tuple[
        dict[tuple[str, str], list[str]],
        dict[str, list[str]],
    ]:
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
        async with async_session() as db:
            service = SearchService(
                db,
                parallel_hydration=_parallel_work_hydration_supported(),
            )
            service._repository_maps = repository_maps
            return await service._build_work_documents(work_ids)

    async def _stream_works_to_index(
        self,
        client: MeiliClient,
        index_uid: str,
        *,
        batch_size: int,
        lease: str,
        lease_ttl_seconds: int,
        resource_owner: str,
    ) -> tuple[int, int, tuple[dict[tuple[str, str], list[str]], dict[str, list[str]]]]:
        """Walk committed works by UUID keyset and release each batch promptly."""

        batch_size = max(1, min(int(batch_size), WORK_DOCUMENT_BATCH_SIZE))
        repository_maps = await self._committed_repository_maps()
        last_id: UUID | None = None
        indexed = 0
        batches = 0
        while True:
            async def _slice(limits: Any) -> tuple[tuple[UUID, ...], int]:
                effective_batch = max(1, min(batch_size, int(limits.work_units)))
                async with async_session() as db:
                    statement = (
                        select(Work.id).order_by(Work.id).limit(effective_batch)
                    )
                    if last_id is not None:
                        statement = statement.where(Work.id > last_id)
                    identities = tuple(
                        (await db.execute(statement)).scalars().all()
                    )
                    if not identities:
                        return (), 0
                    batch_service = SearchService(
                        db,
                        parallel_hydration=_parallel_work_hydration_supported(),
                    )
                    batch_service._repository_maps = repository_maps
                    documents = await batch_service._build_work_documents(
                        identities
                    )
                documents_by_id = {
                    str(document["id"]): document for document in documents
                }
                ordered_documents = [
                    documents_by_id[str(identity)]
                    for identity in identities
                    if str(identity) in documents_by_id
                ]
                first_payload = next(
                    iter(_document_batches(ordered_documents)),
                    [],
                )
                selected_ids = {
                    str(document["id"]) for document in first_payload
                }
                processed_identities: list[UUID] = []
                for identity in identities:
                    document = documents_by_id.get(str(identity))
                    if document is not None and str(identity) not in selected_ids:
                        break
                    processed_identities.append(identity)
                if not processed_identities:
                    raise RuntimeError("Search work slice made no keyset progress")
                await asyncio.to_thread(
                    _write_document_batch,
                    client,
                    index_uid,
                    first_payload,
                )
                return tuple(processed_identities), len(first_payload)

            identities, document_count = await _run_profiled_search_slice(
                resource_owner,
                _slice,
                max_work_units=batch_size,
            )
            if not identities:
                break
            indexed += document_count
            batches += 1
            last_id = identities[-1]
            del identities
            await self._renew_index_write_lease(lease, lease_ttl_seconds)
        return indexed, batches, repository_maps

    async def _stream_reference_to_index(
        self,
        client: MeiliClient,
        live_index: str,
        staging_index: str,
        *,
        batch_size: int,
        lease: str,
        lease_ttl_seconds: int,
        resource_owner: str,
    ) -> tuple[int, int]:
        """Keyset-build a reference projection without retaining all documents."""

        specifications = {
            CREATORS_INDEX: (Creator, "_build_creator_documents"),
            TAGS_INDEX: (Tag, "_build_tag_documents"),
            REPOSITORIES_INDEX: (SubscriptionSource, "_build_repository_documents"),
            SUBSCRIPTIONS_INDEX: (Subscription, "_build_subscription_documents"),
        }
        model, builder_name = specifications[live_index]
        batch_size = max(1, min(int(batch_size), WORK_DOCUMENT_BATCH_SIZE))
        last_id: UUID | None = None
        indexed = 0
        batches = 0
        while True:
            async def _slice(limits: Any) -> tuple[tuple[UUID, ...], int]:
                effective_batch = max(1, min(batch_size, int(limits.work_units)))
                async with async_session() as db:
                    statement = (
                        select(model.id).order_by(model.id).limit(effective_batch)
                    )
                    if last_id is not None:
                        statement = statement.where(model.id > last_id)
                    identities = tuple(
                        (await db.execute(statement)).scalars().all()
                    )
                    if not identities:
                        return (), 0
                    documents = await getattr(
                        SearchService(db), builder_name
                    )(identities)
                documents_by_id = {
                    str(document["id"]): document for document in documents
                }
                ordered_documents = [
                    documents_by_id[str(identity)]
                    for identity in identities
                    if str(identity) in documents_by_id
                ]
                first_payload = next(
                    iter(_document_batches(ordered_documents)),
                    [],
                )
                selected_ids = {
                    str(document["id"]) for document in first_payload
                }
                processed_identities: list[UUID] = []
                for identity in identities:
                    document = documents_by_id.get(str(identity))
                    if document is not None and str(identity) not in selected_ids:
                        break
                    processed_identities.append(identity)
                if not processed_identities:
                    raise RuntimeError("Search reference slice made no keyset progress")
                await asyncio.to_thread(
                    _write_document_batch,
                    client,
                    staging_index,
                    first_payload,
                )
                return tuple(processed_identities), len(first_payload)

            identities, document_count = await _run_profiled_search_slice(
                resource_owner,
                _slice,
                max_work_units=batch_size,
            )
            if not identities:
                break
            indexed += document_count
            batches += 1
            last_id = identities[-1]
            del identities
            await self._renew_index_write_lease(lease, lease_ttl_seconds)
        return indexed, batches

    async def _replay_work_events_to_staging(
        self,
        client: MeiliClient,
        index_uid: str,
        *,
        since: datetime,
        lease: str,
        lease_ttl_seconds: int,
        resource_owner: str,
    ) -> tuple[BinaryIO, int]:
        """Apply mutations concurrent with the keyset walk before swapping."""

        replay_log = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        replayed_count = 0
        cursor: tuple[datetime, UUID] | None = None
        try:
            for _ in range(500_001):
                async def _slice(limits: Any) -> list[ProjectionEvent]:
                    events = await replay_projection_events(
                        WORKS_INDEX,
                        since=since,
                        after=cursor,
                        limit=max(
                            1,
                            min(
                                WORK_DOCUMENT_BATCH_SIZE,
                                int(limits.work_units),
                            ),
                        ),
                    )
                    if not events:
                        return []
                    upsert_events = [
                        event for event in events if event.action == "upsert"
                    ]
                    # Repository membership may change while a long rebuild is
                    # streaming. Reload it for every replay slice so a matching
                    # outbox version cannot be acknowledged with the map that
                    # was cached at rebuild start.
                    current_repository_maps = (
                        await self._committed_repository_maps()
                        if upsert_events
                        else ({}, {})
                    )
                    documents = (
                        await self._committed_work_documents(
                            [UUID(event.entity_id) for event in upsert_events],
                            current_repository_maps,
                        )
                        if upsert_events
                        else []
                    )
                    selected, _deferred = _select_projection_event_slice(
                        events,
                        {WORKS_INDEX: documents},
                    )
                    selected_upserts = {
                        event.entity_id
                        for event in selected
                        if event.action == "upsert"
                    }
                    documents = [
                        document
                        for document in documents
                        if str(document["id"]) in selected_upserts
                    ]
                    present_ids = {
                        str(document["id"]) for document in documents
                    }
                    delete_ids = [
                        event.entity_id
                        for event in selected
                        if event.action == "delete"
                        or (
                            event.action == "upsert"
                            and event.entity_id not in present_ids
                        )
                    ]
                    await asyncio.to_thread(
                        _write_document_batch,
                        client,
                        index_uid,
                        documents,
                    )
                    await asyncio.to_thread(
                        _delete_document_batch,
                        client,
                        index_uid,
                        delete_ids,
                    )
                    return selected

                events = await _run_profiled_search_slice(
                    resource_owner,
                    _slice,
                    max_work_units=WORK_DOCUMENT_BATCH_SIZE,
                )
                if not events:
                    break
                for event in events:
                    replay_log.write(
                        _REBUILD_REPLAY_RECORD.pack(event.id.bytes, event.version)
                    )
                replayed_count += len(events)
                cursor = (events[-1].updated_at, events[-1].id)
                await self._renew_index_write_lease(lease, lease_ttl_seconds)
                if replayed_count > 500_000:
                    raise RuntimeError(
                        "Search outbox replay exceeded 500,000 events"
                    )
            else:
                raise RuntimeError("Search outbox replay exceeded 500,000 events")
            replay_log.flush()
            replay_log.seek(0)
            return replay_log, replayed_count
        except BaseException:
            replay_log.close()
            raise

    async def _rebuild_selected_indexes(
        self,
        live_indexes: tuple[str, ...],
        *,
        batch_size: int = WORK_DOCUMENT_BATCH_SIZE,
        resource_owner: str | None = None,
    ) -> dict[str, Any]:
        """Build isolated indexes with bounded memory, then atomically swap."""

        lease_ttl_seconds = 2 * 60 * 60
        client = _client(timeout_seconds=MEILI_WRITE_TIMEOUT_SECONDS)
        lease = await asyncio.to_thread(
            cache_try_lock,
            INDEX_WRITE_LOCK,
            lease_ttl_seconds,
        )
        if lease is None:
            raise RuntimeError("Another search writer or rebuild is active")

        heartbeat_stop = asyncio.Event()
        writer_lease_lost = asyncio.Event()

        async def _writer_lease_heartbeat() -> None:
            interval = max(30.0, min(60.0, lease_ttl_seconds / 3))
            while not heartbeat_stop.is_set():
                try:
                    await asyncio.wait_for(heartbeat_stop.wait(), timeout=interval)
                    return
                except TimeoutError:
                    pass
                try:
                    renewed = await asyncio.to_thread(
                        cache_refresh_lock,
                        INDEX_WRITE_LOCK,
                        lease,
                        lease_ttl_seconds,
                    )
                except Exception:
                    # Ownership is now unverifiable.  Fail closed at the next
                    # slice boundary; never allow this rebuild to swap after a
                    # second writer may have acquired an expired key.
                    logger.exception("Search rebuild writer lease heartbeat failed")
                    writer_lease_lost.set()
                    return
                if not renewed:
                    logger.error("Search rebuild lost its writer lease")
                    writer_lease_lost.set()
                    return

        async def _assert_writer_lease() -> None:
            if writer_lease_lost.is_set():
                raise RuntimeError("Lost search index writer lease during rebuild")
            try:
                await self._renew_index_write_lease(lease, lease_ttl_seconds)
            except Exception:
                writer_lease_lost.set()
                raise

        writer_heartbeat = asyncio.create_task(_writer_lease_heartbeat())

        started_at = datetime.now(timezone.utc)
        started_clock = monotonic_time.perf_counter()
        generation = uuid4().hex[:12]
        profile_owner = resource_owner or f"search-rebuild-{generation}"
        staging = {
            live_index: f"{live_index}__staging_{generation}"
            for live_index in live_indexes
        }
        stats: dict[str, int] = {live_index: 0 for live_index in live_indexes}
        work_batches = 0
        replay_log: BinaryIO | None = None
        replayed_count = 0
        try:
            for live_index, staging_index in staging.items():
                async def _create_slice(
                    _limits: Any,
                    *,
                    current_live: str = live_index,
                    current_staging: str = staging_index,
                ) -> None:
                    await _assert_writer_lease()
                    await asyncio.to_thread(
                        _create_staging_index,
                        client,
                        current_live,
                        current_staging,
                    )

                await _run_profiled_search_slice(
                    profile_owner,
                    _create_slice,
                    max_work_units=1,
                )
                await self._renew_index_write_lease(lease, lease_ttl_seconds)

            repository_maps = None
            if WORKS_INDEX in live_indexes:
                stats[WORKS_INDEX], work_batches, repository_maps = (
                    await self._stream_works_to_index(
                        client,
                        staging[WORKS_INDEX],
                        batch_size=batch_size,
                        lease=lease,
                        lease_ttl_seconds=lease_ttl_seconds,
                        resource_owner=profile_owner,
                    )
                )

            for live_index in live_indexes:
                if live_index == WORKS_INDEX:
                    continue
                reference_count, _reference_batches = (
                    await self._stream_reference_to_index(
                        client,
                        live_index,
                        staging[live_index],
                        batch_size=batch_size,
                        lease=lease,
                        lease_ttl_seconds=lease_ttl_seconds,
                        resource_owner=profile_owner,
                    )
                )
                stats[live_index] = reference_count

            if WORKS_INDEX in live_indexes:
                assert repository_maps is not None
                replay_log, replayed_count = (
                    await self._replay_work_events_to_staging(
                    client,
                    staging[WORKS_INDEX],
                    since=started_at,
                    lease=lease,
                    lease_ttl_seconds=lease_ttl_seconds,
                    resource_owner=profile_owner,
                    )
                )

            await self._renew_index_write_lease(lease, lease_ttl_seconds)

            def _swap() -> None:
                for live_index in live_indexes:
                    _ensure_live_index_for_swap(client, live_index)
                _wait_for_task(
                    client,
                    client.swap_indexes([
                        (live_index, staging[live_index])
                        for live_index in live_indexes
                    ]),
                    timeout_in_ms=MEILI_TASK_TIMEOUT_MS,
                )
                _remember_live_settings(live_indexes, client)

            async def _swap_slice(_limits: Any) -> None:
                # Validate the fencing token only after the maintenance
                # barrier is held.  A long wait for other slices therefore
                # cannot end in a stale rebuild swapping over a newer writer.
                await _assert_writer_lease()
                await asyncio.to_thread(_swap)

            # Swapping the staging indexes is the only rebuild phase that must
            # exclude every other disk profile.  The maintenance permit is
            # deliberately held only for this short atomic cut-over.
            await _run_profiled_search_slice(
                profile_owner,
                _swap_slice,
                workload="maintenance",
                max_work_units=1,
            )
            if replay_log is not None:
                while payload := replay_log.read(
                    _REBUILD_REPLAY_RECORD.size * WORK_DOCUMENT_BATCH_SIZE
                ):
                    if len(payload) % _REBUILD_REPLAY_RECORD.size:
                        raise RuntimeError("Corrupt search rebuild replay log")
                    versions = [
                        (
                            UUID(bytes=event_id),
                            int(version),
                        )
                        for event_id, version in _REBUILD_REPLAY_RECORD.iter_unpack(
                            payload
                        )
                    ]
                    await complete_projection_versions(versions)
            await prune_completed_projection_events(retention_hours=24)
            await _refresh_search_index_checkpoints(live_indexes)
            return {
                "status": "ok",
                "counts": stats,
                "batches": work_batches,
                "replayed": replayed_count,
                "seconds": round(monotonic_time.perf_counter() - started_clock, 1),
            }
        finally:
            try:
                if not writer_lease_lost.is_set():
                    for staging_index in staging.values():
                        async def _cleanup_slice(
                            _limits: Any,
                            *,
                            current_staging: str = staging_index,
                        ) -> None:
                            await _assert_writer_lease()
                            await asyncio.to_thread(
                                _drop_index_best_effort,
                                client,
                                current_staging,
                            )

                        try:
                            await _run_profiled_search_slice(
                                profile_owner,
                                _cleanup_slice,
                                max_work_units=1,
                            )
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            # Never perform an ungoverned delete just to clean
                            # a failed rebuild.  A uniquely named orphan is safe
                            # and can be reclaimed by a later run.
                            logger.warning(
                                "Unable to clean staging search index %s",
                                staging_index,
                                exc_info=True,
                            )
            finally:
                if replay_log is not None:
                    replay_log.close()
                heartbeat_stop.set()
                writer_heartbeat.cancel()
                try:
                    await writer_heartbeat
                except asyncio.CancelledError:
                    pass
                await asyncio.to_thread(
                    cache_release_lock,
                    INDEX_WRITE_LOCK,
                    lease,
                )

    async def refresh_reference_indexes(self) -> None:
        """Atomically refresh reference projections with bounded peak memory."""

        try:
            await self._rebuild_selected_indexes((
                CREATORS_INDEX,
                TAGS_INDEX,
                REPOSITORIES_INDEX,
                SUBSCRIPTIONS_INDEX,
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
                "status": "ok",
                "total": int(result["counts"][WORKS_INDEX]),
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
                    ids=batch_ids,
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
        return {"status": "drift" if has_drift else "ok", "indexes": indexes}

    async def reindex(self, *, resource_owner: str | None = None) -> dict:
        try:
            result = await self._rebuild_selected_indexes((
                WORKS_INDEX,
                CREATORS_INDEX,
                TAGS_INDEX,
                REPOSITORIES_INDEX,
                SUBSCRIPTIONS_INDEX,
            ), resource_owner=resource_owner)
        except Exception as exc:
            logger.exception("Search reindex failed")
            return {
                "status": "error",
                "message": str(exc),
            }
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
