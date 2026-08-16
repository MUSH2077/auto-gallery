from app.services.image_utils import can_generate_thumbnail, get_mime_type, can_compute_phash, IMAGE_EXTS
from app.services.media_assets import browser_video_mime_type, render_video_derivatives
import asyncio
from collections import deque
from contextlib import AsyncExitStack, asynccontextmanager
import hashlib
import json
import logging
import os
from time import monotonic
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session
from app.jobs.import_outcome import classify_import_outcome, clamp_threshold
from app.jobs.import_list_cleanup import cleanup_import_list, prune_import_lists
from app.jobs.worker_control import ControlListener, HeartbeatPublisher
from app.models import (
    Work, WorkSource, Asset, AssetSource, Tag, WorkTag, WorkSourceTag, SourceCreator,
    CreatorCurationState, ImportCurationOutbox, MediaDerivativeOutbox,
    MaintenanceAuditEvent, SearchProjectionOutbox, WorkCurationState,
)
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.import_job import ImportJob
from app.models.download_job import DownloadJob
from app.providers import registry
from app.repositories.download_job import DownloadJobRepository
from app.services.job_progress import apply_download_progress, apply_import_progress
from app.services.job_manifest import append_manifest_event, update_manifest
from app.services.download_finalization import finalize_download_job
from app.services.import_lifecycle import project_import_pipeline_state
from app.models.task_state import transition_import_job
from app.services.settings import get_download_defaults
from app.services.import_dispatch import prepare_import_dispatch, publish_prepared_import
from app.services.sync_outcome import build_sync_outcome
from app.services.work_import import WorkImportService
from app.services.artifact_discovery import group_metadata_by_work, media_files_for_group
from app.services.heavy_io import (
    HeavyIOUnavailable,
    heavy_io_slot,
    set_resource_state,
    wait_for_resource_capacity,
)
from app.services.media_derivatives import MEDIA_ALGORITHM_VERSION
from app.services.media_derivatives import request_media_derivatives
from app.services.import_projection import request_import_projection
from app.services.resource_pressure import (
    current_profile_slice_limits,
    sleep_for_profile_slice_cooldown,
)
from app.services.search_projection_outbox import (
    DEFAULT_CREATORS_INDEX_UID,
    DEFAULT_REPOSITORIES_INDEX_UID,
    DEFAULT_SUBSCRIPTIONS_INDEX_UID,
    DEFAULT_TAGS_INDEX_UID,
    DEFAULT_WORKS_INDEX_UID,
    mark_works_generation_pending,
    request_search_projection,
)
from app.services.stage_metrics import measure_async_stage

logger = logging.getLogger(__name__)

ARCHIVE_EXTENSIONS = {".zip"}
VIDEO_EXTS = {".mp4", ".webm"}
ASSET_EXTENSIONS = IMAGE_EXTS | ARCHIVE_EXTENSIONS | VIDEO_EXTS
IMPORT_WORK_BATCH_SIZE = 25
IMPORT_ARTIFACT_LEASE_SECONDS = 900

# Backward-compatible private aliases used by older extensions and tests.
_can_generate_thumbnail = can_generate_thumbnail
_can_compute_phash = can_compute_phash
_mime_type = get_mime_type


def _should_auto_link_creator(
    provider: Any,
    first_raw: dict,
    subscription_source: Any | None,
) -> tuple[bool, str | None, str | None]:
    """Determine whether a new SourceCreator should be auto-linked to a subscription's creator.

    Returns (should_link, url_creator, meta_creator).

    Only returns should_link=True when the creator identity extracted from the
    subscription source URL matches the creator identity parsed from the work
    metadata.  Mismatch occurs with retweets, reposts, bookmarked/favorited
    content from other creators — these are left unlinked for admin review.
    """
    if not subscription_source or not subscription_source.source_url:
        return False, None, None

    try:
        _prov = registry.get(subscription_source.source)
        url_creator = _prov.get_creator_dir_from_url(subscription_source.source_url)
    except KeyError:
        return False, None, None

    if url_creator is None:
        return False, url_creator, None

    meta_creator = provider.get_creator_directory_name(first_raw)
    if not meta_creator:
        return False, url_creator, meta_creator

    return str(url_creator) == str(meta_creator), url_creator, meta_creator


def _parse_posted_at(value: Any) -> datetime | None:
    """Normalize provider date values to timezone-aware datetimes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_posted_at(int(text))
        normalized = text.replace("Z", "+00:00")
        for candidate in (
            normalized,
            normalized.replace(" ", "T", 1),
        ):
            try:
                parsed = datetime.fromisoformat(candidate)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed_date = datetime.strptime(text, fmt)
                if fmt == "%Y-%m-%d":
                    parsed_date = datetime.combine(parsed_date.date(), time.min)
                return parsed_date.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    return None


def _posted_at_json(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _detect_ai_generated(raw: dict, source: str) -> bool:
    """Detect if a work is AI-generated from its raw metadata."""
    # Pixiv: illust_ai_type field (1=AI, 2=AI)
    if source == "pixiv":
        ai_type = raw.get("illust_ai_type")
        if ai_type is not None and ai_type == 2:
            return True
    # Danbooru: check meta tags for ai_generated
    if source == "danbooru":
        meta_tags = raw.get("tag_string_meta", "")
        if "ai_generated" in meta_tags.lower():
            return True
    # Generic tag-based detection for other sources
    tags_str = " ".join(str(v) for v in raw.values() if isinstance(v, str)).lower()
    ai_indicators = ["ai_generated", "ai-generated", "ai generated", "created by ai", "ai art"]
    for indicator in ai_indicators:
        if indicator in tags_str:
            return True
    return False


def _detect_nsfw(raw: dict, source: str) -> bool:
    """Detect if a work is NSFW/R-18 from its raw metadata.

    Pixiv field reference (from gallery-dl source, pixiv.py):
      x_restrict: 0=General, 1=R-18, 2=R-18G
      rating: gallery-dl computed from x_restrict ("General"/"R-18"/"R-18G")
      sanity_level: 0-11, 6+ restricted, 7+ explicit (NOT reliable alone)
    """
    if source == "pixiv":
        # Primary: gallery-dl's computed rating (most reliable)
        rating = raw.get("rating", "")
        if rating in ("R-18", "R-18G"):
            return True
        # Fallback: raw Pixiv API x_restrict field
        xr = raw.get("x_restrict")
        if xr is not None and isinstance(xr, (int, float)) and xr >= 1:
            return True
        # Backward compat: some JSON formats may have 'restrict' as x_restrict
        restrict = raw.get("restrict")
        if restrict is not None and isinstance(restrict, (int, float)) and restrict >= 1:
            return True
    elif source == "danbooru":
        # rating: s=safe, q=questionable, e=explicit
        rating = raw.get("rating", "").lower()
        if rating in ("q", "e"):
            return True
    elif source == "iwara":
        rating = raw.get("rating", "").lower()
        if rating and rating not in ("general", "allages", "all-ages", "safe"):
            return True
    elif source == "x":
        if (
            raw.get("possibly_sensitive")
            or raw.get("sensitive")
            or bool(raw.get("sensitive_flags"))
        ):
            return True
    elif source == "manual":
        # ManualUploadService (app/services/manual_upload.py) writes the
        # uploader-supplied flag straight onto the metadata.json top level --
        # there is no upstream platform signal to derive it from.
        return bool(raw.get("is_nsfw", False))
    return False


def _consume_import_file_list(redis_client, import_job_id: str) -> list[str] | None:
    """Read and delete the exact JSON file list captured by the download job.

    Checks Redis first (fast path), then falls back to a durable on-disk
    copy that survives Redis restarts and long queue delays.
    """
    files_key = f"import:{import_job_id}:files"
    files_raw = redis_client.get(files_key)
    if files_raw:
        new_json_paths = json.loads(files_raw)
        redis_client.delete(files_key)
        # Also clean up the disk fallback if it exists
        try:
            fallback_path = Path(settings.download_root) / ".import-lists" / f"{import_job_id}.json"
            if fallback_path.exists():
                fallback_path.unlink()
        except Exception:
            pass
        return new_json_paths

    # Fallback: check durable on-disk copy (survives Redis restarts, long queues)
    try:
        fallback_path = Path(settings.download_root) / ".import-lists" / f"{import_job_id}.json"
        if fallback_path.exists():
            data = json.loads(fallback_path.read_text())
            fallback_path.unlink()  # consume it
            logger.info("Import %s: loaded file list from disk fallback (%d files)",
                       import_job_id, len(data))
            return data
    except Exception:
        logger.warning("Failed to read import file list fallback for %s", import_job_id, exc_info=True)

    return None


def _chunked(values: list[Any], size: int = 500):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _normalize_tag_value(value: Any) -> str:
    """Return a deterministic value that fits the production VARCHAR(200)."""

    normalized = str(value or "").lower().strip()
    if len(normalized) <= 200:
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"{normalized[:191]}-{digest}"


async def _existing_work_source_ids(source: str, source_work_ids: list[str]) -> set[str]:
    existing: set[str] = set()
    async with async_session() as db:
        for chunk in _chunked(source_work_ids, 1000):
            existing.update(
                (
                    await db.execute(
                        select(WorkSource.source_work_id).where(
                            WorkSource.source == source,
                            WorkSource.source_work_id.in_(chunk),
                        )
                    )
                ).scalars()
            )
    return existing


async def _prepare_import_metadata(provider, download_job, groups: dict) -> tuple[dict, dict]:
    """Parse once, then bulk-upsert shared creators and tags.

    The returned mapping is read-only during the per-work write loop.  No
    SourceCreator or Tag existence query is performed inside that loop.
    """
    prepared: dict[str, dict[str, Any]] = {}
    failures: dict[str, str] = {}
    tag_specs: dict[str, str | None] = {}
    creator_specs: dict[tuple[str, str], dict[str, Any]] = {}
    for source_work_id, items in groups.items():
        if not items:
            failures[source_work_id] = "metadata group is empty"
            continue
        first_file, first_raw = items[0]
        try:
            ws_data = provider.parse_work_source(first_raw)
            sc_data = provider.parse_source_creator(first_raw)
            parsed_tags: list[dict[str, Any]] = []
            try:
                seen: set[str] = set()
                for value in provider.parse_source_tags(first_raw):
                    normalized = _normalize_tag_value(value.get("original_name"))
                    if not normalized or normalized in seen:
                        continue
                    seen.add(normalized)
                    category = value.get("category")
                    parsed_tags.append(
                        {
                            "normalized_name": normalized,
                            "original_name": value.get("original_name"),
                            "category": category,
                        }
                    )
                    tag_specs.setdefault(normalized, category)
                parsed_tags.sort(key=lambda item: item["normalized_name"])
            except NotImplementedError:
                pass
            creator_key = (
                str(sc_data["source"]),
                str(sc_data["source_creator_id"]),
            )
            creator_specs.setdefault(
                creator_key,
                {"data": sc_data, "raw": first_raw},
            )
            prepared[source_work_id] = {
                "items": items,
                "first_file": first_file,
                "first_raw": first_raw,
                "ws_data": ws_data,
                "sc_data": sc_data,
                "creator_key": creator_key,
                "posted_at": _parse_posted_at(ws_data.get("posted_at")),
                "tags": parsed_tags,
            }
        except Exception:
            logger.warning(
                "Failed to parse provider data for %s/%s",
                provider.source_name,
                source_work_id,
                exc_info=True,
            )
            failures[source_work_id] = "provider metadata parse failed"

    async with async_session() as db:
        subscription_source = (
            await db.get(SubscriptionSource, download_job.subscription_source_id)
            if download_job.subscription_source_id
            else None
        )
        subscription = (
            await db.get(Subscription, download_job.subscription_id)
            if download_job.subscription_id
            else None
        )
        creator_keys = sorted(creator_specs)
        creator_rows: dict[tuple[str, str], SourceCreator] = {}
        for chunk in _chunked(creator_keys):
            rows = (
                await db.execute(
                    select(SourceCreator).where(
                        tuple_(
                            SourceCreator.source,
                            SourceCreator.source_creator_id,
                        ).in_(chunk)
                    )
                )
            ).scalars()
            creator_rows.update(
                {(row.source, row.source_creator_id): row for row in rows}
            )

        missing_values: list[dict[str, Any]] = []
        link_targets: dict[tuple[str, str], UUID | None] = {}
        for key in creator_keys:
            spec = creator_specs[key]
            should_link, _, _ = _should_auto_link_creator(
                provider, spec["raw"], subscription_source
            )
            linked_creator = (
                subscription.creator_id
                if should_link and subscription and subscription.creator_id
                else None
            )
            link_targets[key] = linked_creator
            if key not in creator_rows:
                data = spec["data"]
                missing_values.append(
                    {
                        "id": uuid4(),
                        "source": data["source"],
                        "source_creator_id": data["source_creator_id"],
                        "source_url": data.get("source_url"),
                        "display_name": data.get("display_name"),
                        "creator_id": linked_creator,
                    }
                )
        for chunk in _chunked(missing_values):
            if chunk:
                await db.execute(
                    insert(SourceCreator)
                    .values(chunk)
                    .on_conflict_do_nothing(
                        constraint="uq_source_creators_source_id"
                    )
                )
        if missing_values:
            for chunk in _chunked(creator_keys):
                rows = (
                    await db.execute(
                        select(SourceCreator).where(
                            tuple_(
                                SourceCreator.source,
                                SourceCreator.source_creator_id,
                            ).in_(chunk)
                        )
                    )
                ).scalars()
                creator_rows.update(
                    {(row.source, row.source_creator_id): row for row in rows}
                )
        for key, creator in creator_rows.items():
            if creator.creator_id is None and link_targets.get(key) is not None:
                creator.creator_id = link_targets[key]
        if subscription_source and not subscription_source.source_creator_id:
            matching = next(
                (
                    key[1]
                    for key, linked_creator in link_targets.items()
                    if linked_creator is not None
                    and key[0] == subscription_source.source
                ),
                None,
            )
            if matching:
                subscription_source.source_creator_id = matching

        tag_names = sorted(tag_specs)
        existing_tags: dict[str, UUID] = {}
        for chunk in _chunked(tag_names, 1000):
            rows = await db.execute(
                select(Tag.id, Tag.normalized_name).where(
                    Tag.normalized_name.in_(chunk)
                )
            )
            existing_tags.update({name: tag_id for tag_id, name in rows})
        missing_tags = [
            {
                "id": uuid4(),
                "normalized_name": name,
                "category": tag_specs[name],
            }
            for name in tag_names
            if name not in existing_tags
        ]
        for chunk in _chunked(missing_tags, 1000):
            if chunk:
                await db.execute(
                    insert(Tag)
                    .values(chunk)
                    .on_conflict_do_nothing(index_elements=["normalized_name"])
                )
        if missing_tags:
            for chunk in _chunked(tag_names, 1000):
                rows = await db.execute(
                    select(Tag.id, Tag.normalized_name).where(
                        Tag.normalized_name.in_(chunk)
                    )
                )
                existing_tags.update({name: tag_id for tag_id, name in rows})

        linked_creator_ids = {
            creator.creator_id
            for creator in creator_rows.values()
            if creator.creator_id is not None
        }
        archived_creator_ids: set[UUID] = set()
        if linked_creator_ids:
            archived_creator_ids.update(
                (
                    await db.execute(
                        select(CreatorCurationState.creator_id).where(
                            CreatorCurationState.creator_id.in_(linked_creator_ids),
                            CreatorCurationState.visibility == "archived",
                        )
                    )
                ).scalars()
            )
        await db.commit()

    for item in prepared.values():
        creator = creator_rows[item["creator_key"]]
        item["linked_creator_id"] = creator.creator_id
        item["creator_archived"] = creator.creator_id in archived_creator_ids
        for tag in item["tags"]:
            tag["id"] = existing_tags[tag["normalized_name"]]
    return prepared, failures


def _prepared_work_batches(
    prepared: dict[str, dict[str, Any]],
    batch_size: int = IMPORT_WORK_BATCH_SIZE,
):
    """Yield stable, bounded work batches without copying provider payloads."""

    bounded_size = max(1, min(int(batch_size), IMPORT_WORK_BATCH_SIZE))
    items = list(prepared.items())
    for offset in range(0, len(items), bounded_size):
        yield items[offset : offset + bounded_size]


def _take_prepared_work_slice(
    prepared_batch: list[tuple[str, dict[str, Any]]],
    work_units: int,
) -> tuple[list[tuple[str, dict[str, Any]]], list[tuple[str, dict[str, Any]]]]:
    """Split one queued batch at the current adaptive import budget."""

    bounded_units = max(1, min(int(work_units), IMPORT_WORK_BATCH_SIZE))
    return prepared_batch[:bounded_units], prepared_batch[bounded_units:]


@asynccontextmanager
async def _import_resource_slice(
    workload: str,
    owner: str,
    *,
    source_identity: str | None = None,
    wait_for_capacity: bool = True,
):
    """Yield current limits under one lease, then cool down lock-free."""

    attempt = 0
    stack: AsyncExitStack | None = None
    pressure_snapshot: dict[str, Any] = {}
    limits = None
    while stack is None:
        limits, pressure_snapshot = await current_profile_slice_limits(workload)
        if not limits.allowed:
            if not wait_for_capacity:
                yield None
                return
            # This event-driven wait owns neither a DB session nor a flock.  A
            # fresh snapshot is resolved on the next loop so recovery cannot
            # accidentally reuse the critical slice's zero-unit budget.
            await wait_for_resource_capacity(workload=workload, owner=owner)
            continue
        candidate = AsyncExitStack()
        try:
            await candidate.enter_async_context(
                heavy_io_slot(
                    workload,
                    owner,
                    source_identity=source_identity,
                )
            )
        except HeavyIOUnavailable as error:
            await candidate.aclose()
            await set_resource_state(
                owner,
                "waiting",
                error.reason,
                workload=workload,
            )
            if not wait_for_capacity:
                yield None
                return
            await asyncio.sleep(min(30.0, 2.0 ** min(attempt + 1, 5)))
            attempt += 1
        else:
            stack = candidate
    assert stack is not None and limits is not None
    slice_started = monotonic()
    try:
        yield limits
    finally:
        elapsed_seconds = monotonic() - slice_started
        await stack.aclose()
        await set_resource_state(
            owner,
            "yielded",
            "slice_complete",
            workload=workload,
        )
        # Batch shrinking alone does not constrain sustained backlog.  This
        # enforce-only duty-cycle delay runs after every transaction and both
        # Redis/POSIX leases have been released; shadow mode returns zero.
        await sleep_for_profile_slice_cooldown(
            pressure_snapshot,
            elapsed_seconds=elapsed_seconds,
            max_seconds=300.0,
            workload=workload,
        )


def _prepared_media_workload(prepared_work: dict[str, Any]) -> str:
    """Choose the profile for only the first-card synchronous derivative."""

    first_file = prepared_work.get("first_file")
    if first_file is not None and Path(first_file).suffix.lower() in VIDEO_EXTS:
        return "video_derive"
    return "image_derive"


class _ArtifactLeaseLost(RuntimeError):
    """The current execution cannot verify its artifact batch lease."""


class _ArtifactBatchLease:
    """In-memory view of identities retained by the last durable batch CAS."""

    def __init__(self, source_work_ids: list[str]) -> None:
        self.owned = set(source_work_ids)

    def retain(self, source_work_ids: set[str]) -> None:
        self.owned.intersection_update(source_work_ids)

    def owns(self, source_work_id: str) -> bool:
        return source_work_id in self.owned


@asynccontextmanager
async def _artifact_batch_lease_guard(
    download_job_id: UUID,
    source_work_ids: list[str],
    import_job_id: UUID,
    execution_token: UUID,
):
    """Renew one claimed slice with one SQL statement per heartbeat."""

    from app.services.artifact_ledger import ArtifactLedger

    lease = _ArtifactBatchLease(source_work_ids)

    async def renew_once() -> None:
        if not lease.owned:
            return
        async with async_session() as lease_db:
            owned = await ArtifactLedger(lease_db).renew_work_leases(
                download_job_id,
                sorted(lease.owned),
                expected_import_job_id=import_job_id,
                expected_lease_token=execution_token,
                lease_seconds=IMPORT_ARTIFACT_LEASE_SECONDS,
            )
            await lease_db.commit()
        lease.retain(owned)

    try:
        await renew_once()
    except Exception as error:
        raise _ArtifactLeaseLost(
            f"artifact lease verification unavailable: {type(error).__name__}"
        ) from error

    stop = asyncio.Event()

    async def renew_loop() -> None:
        delay = min(60.0, max(5.0, IMPORT_ARTIFACT_LEASE_SECONDS / 3))
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            try:
                await renew_once()
                if not lease.owned:
                    return
                delay = min(
                    60.0,
                    max(5.0, IMPORT_ARTIFACT_LEASE_SECONDS / 3),
                )
            except Exception:
                # A transient database failure is not evidence that another
                # execution won the CAS. Retry quickly while the last durable
                # expiry remains authoritative.
                logger.warning(
                    "Artifact batch lease renewal failed for import %s",
                    import_job_id,
                    exc_info=True,
                )
                delay = 5.0

    renewal_task = asyncio.create_task(renew_loop())
    try:
        yield lease
    finally:
        stop.set()
        await renewal_task


async def _prepare_work_media(
    provider: Any,
    prepared_work: dict[str, Any],
    source_work_id: str,
    *,
    derive_primary: bool = True,
) -> dict[str, Any]:
    """Perform filesystem inspection and the first-card derive outside SQL."""

    items = prepared_work["items"]
    first_file = prepared_work["first_file"]
    first_raw = prepared_work["first_raw"]
    sc_data = prepared_work["sc_data"]
    dir_name = provider.get_creator_directory_name(first_raw)
    dir_name = dir_name.replace("/", "_").replace("\\", "_").strip()
    display_name = sc_data.get("display_name") or dir_name
    asset_files = media_files_for_group(items, source_work_id)
    if not asset_files:
        raise ValueError("no media assets found")

    creator_dir, lib_dir = WorkImportService.library_directory(
        provider.source_name,
        first_raw,
        source_work_id,
    )
    lib_dir.mkdir(parents=True, exist_ok=True)
    file_stats = {path: path.stat() for path in asset_files}

    # The first card asset is immediately visible. All non-card derivatives,
    # hashes and deduplication are durable outbox requests after the SQL batch.
    primary_values: dict[str, Any] = {}
    primary = asset_files[0]
    if derive_primary and can_generate_thumbnail(primary.suffix):
        from app.services.thumbnail import inspect_and_generate_thumbnail

        thumb, width, height = await asyncio.to_thread(
            inspect_and_generate_thumbnail,
            str(primary),
            lib_dir,
            f"{primary.stem}.thumbnail",
        )
        primary_values.update(width=width, height=height)
        if thumb:
            primary_values["thumb_sm_path"] = str(
                Path(thumb).relative_to(settings.library_root)
            )
    elif derive_primary and primary.suffix.lower() in VIDEO_EXTS:
        derivatives = await asyncio.to_thread(
            render_video_derivatives,
            primary,
            lib_dir,
            primary.stem,
        )
        if derivatives.inspection:
            primary_values.update(
                width=derivatives.inspection.width,
                height=derivatives.inspection.height,
                duration=derivatives.inspection.duration,
                mime_type=browser_video_mime_type(
                    primary.name,
                    get_mime_type(primary.suffix),
                ),
            )
        if derivatives.thumbnail_path:
            primary_values["thumb_sm_path"] = str(
                derivatives.thumbnail_path.relative_to(settings.library_root)
            )
        if derivatives.poster_path:
            primary_values["thumb_lg_path"] = str(
                derivatives.poster_path.relative_to(settings.library_root)
            )
        if derivatives.error:
            logger.warning(
                "Primary video derivatives incomplete for %s: %s",
                primary,
                derivatives.error,
            )

    return {
        "source_work_id": source_work_id,
        "prepared_work": prepared_work,
        "asset_files": asset_files,
        "file_stats": file_stats,
        "primary_values": primary_values,
        "creator_dir": creator_dir,
        "lib_dir": lib_dir,
        "display_name": display_name,
        "work_dir": first_file.parent,
    }


def _asset_role(path: Path) -> str:
    if path.suffix.lower() in VIDEO_EXTS:
        return "video"
    if path.suffix.lower() in ARCHIVE_EXTENSIONS:
        return "archive"
    if path.suffix.lower() == ".gif":
        return "animation"
    return "page"


def _asset_derivative_request(
    asset_id: UUID,
    path: Path,
    stat: os.stat_result,
    *,
    primary_derivative_ready: bool = False,
) -> dict[str, Any]:
    requested: dict[str, bool] = {"sha256": True, "dedup": True}
    if can_compute_phash(path.suffix):
        requested["phash"] = True
    if not primary_derivative_ready:
        if can_generate_thumbnail(path.suffix):
            requested.update(thumbnail=True, dimensions=True)
        elif path.suffix.lower() in VIDEO_EXTS:
            requested.update(thumbnail=True, dimensions=True, video=True)
    return {
        "asset_id": asset_id,
        "requested": requested,
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
    }


async def _update_existing_work_groups(
    provider: Any,
    download_job: DownloadJob,
    prepared: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Refresh existing upstream works and append newly published assets.

    Existing WorkSource rows used to be treated as an unconditional no-op.
    That made a legitimate upstream metadata revision (for example a Pixiv
    work growing from one page to two) land on disk while its database work
    remained stale.  This path locks the authoritative source row, updates its
    metadata, inserts only missing source assets, and commits all projection
    intents and the audit event in the same transaction.
    """

    from app.services.artifact_ledger import ArtifactLedger

    result: dict[str, Any] = {
        "updated": 0,
        "unchanged": 0,
        "assets": 0,
        "multi_page": 0,
        "failures": {},
    }
    download_root = Path(settings.download_root).resolve()

    for prepared_batch in _prepared_work_batches(prepared):
        batch_ids = [source_work_id for source_work_id, _ in prepared_batch]
        prepared_by_id = dict(prepared_batch)
        media_by_id: dict[str, tuple[list[Path], dict[Path, os.stat_result]]] = {}
        for source_work_id, prepared_work in prepared_batch:
            try:
                asset_files = media_files_for_group(
                    prepared_work["items"],
                    source_work_id,
                )
                if not asset_files:
                    raise ValueError("no media assets found")
                for path in asset_files:
                    path.resolve().relative_to(download_root)
                media_by_id[source_work_id] = (
                    asset_files,
                    {path: path.stat() for path in asset_files},
                )
            except Exception as error:
                result["failures"][source_work_id] = _safe_work_error(error)

        successful: list[str] = []
        async with async_session() as db:
            work_sources = list(
                (
                    await db.execute(
                        select(WorkSource)
                        .where(
                            WorkSource.source == provider.source_name,
                            WorkSource.source_work_id.in_(batch_ids),
                        )
                        .order_by(WorkSource.source_work_id, WorkSource.id)
                        .with_for_update()
                    )
                ).scalars()
            )
            work_source_by_id = {
                row.source_work_id: row for row in work_sources
            }

            for source_work_id in batch_ids:
                if source_work_id in result["failures"]:
                    continue
                work_source = work_source_by_id.get(source_work_id)
                if work_source is None:
                    result["failures"][source_work_id] = (
                        "existing WorkSource disappeared during update"
                    )
                    continue
                prepared_work = prepared_by_id[source_work_id]
                ws_data = prepared_work["ws_data"]
                asset_files, file_stats = media_by_id[source_work_id]
                try:
                    async with db.begin_nested():
                        work = (
                            await db.execute(
                                select(Work)
                                .where(Work.id == work_source.work_id)
                                .with_for_update()
                            )
                        ).scalar_one()
                        existing_assets = list(
                            (
                                await db.execute(
                                    select(AssetSource, Asset)
                                    .join(Asset, Asset.id == AssetSource.asset_id)
                                    .where(
                                        AssetSource.work_source_id == work_source.id
                                    )
                                    .order_by(
                                        AssetSource.ordinal.asc().nullslast(),
                                        AssetSource.id,
                                    )
                                    .with_for_update(of=(AssetSource, Asset))
                                )
                            ).all()
                        )
                        existing_source_asset_ids = {
                            row.source_asset_id
                            for row, _asset in existing_assets
                            if row.source_asset_id
                        }
                        occupied_ordinals = {
                            row.ordinal
                            for row, _asset in existing_assets
                            if row.ordinal is not None
                        }

                        changed_fields = [
                            field
                            for field, old, new in (
                                ("source_url", work_source.source_url, ws_data.get("source_url")),
                                (
                                    "source_creator_id",
                                    work_source.source_creator_id,
                                    ws_data.get("source_creator_id"),
                                ),
                                ("title", work_source.title, ws_data.get("title")),
                                (
                                    "description",
                                    work_source.description,
                                    ws_data.get("description"),
                                ),
                                (
                                    "posted_at",
                                    work_source.posted_at,
                                    prepared_work["posted_at"],
                                ),
                                (
                                    "raw_metadata",
                                    work_source.raw_metadata,
                                    ws_data.get("raw_metadata"),
                                ),
                            )
                            if old != new
                        ]
                        old_raw_hash = hashlib.sha256(
                            json.dumps(
                                work_source.raw_metadata or {},
                                sort_keys=True,
                                ensure_ascii=False,
                                default=str,
                            ).encode("utf-8")
                        ).hexdigest()
                        new_raw_hash = hashlib.sha256(
                            json.dumps(
                                ws_data.get("raw_metadata") or {},
                                sort_keys=True,
                                ensure_ascii=False,
                                default=str,
                            ).encode("utf-8")
                        ).hexdigest()
                        work_source.source_url = ws_data.get("source_url")
                        work_source.source_creator_id = ws_data.get(
                            "source_creator_id"
                        )
                        work_source.title = ws_data.get("title")
                        work_source.description = ws_data.get("description")
                        work_source.posted_at = prepared_work["posted_at"]
                        work_source.raw_metadata = ws_data.get("raw_metadata")

                        new_asset_ids: list[UUID] = []
                        derivative_requests: list[dict[str, Any]] = []
                        for ordinal, path in enumerate(asset_files):
                            source_asset_id = path.stem
                            if source_asset_id in existing_source_asset_ids:
                                continue
                            if ordinal in occupied_ordinals:
                                raise ValueError(
                                    "new upstream asset conflicts with an existing ordinal"
                                )
                            stat = file_stats[path]
                            asset_id = uuid4()
                            asset = Asset(
                                id=asset_id,
                                file_name=path.name,
                                file_path=str(path.resolve().relative_to(download_root)),
                                file_size=stat.st_size,
                                mime_type=get_mime_type(path.suffix),
                            )
                            db.add(asset)
                            db.add(
                                AssetSource(
                                    id=uuid4(),
                                    asset_id=asset_id,
                                    work_source_id=work_source.id,
                                    source=provider.source_name,
                                    source_asset_id=source_asset_id,
                                    ordinal=ordinal,
                                    role=_asset_role(path),
                                )
                            )
                            if work.thumbnail_asset_id is None and ordinal == 0:
                                work.thumbnail_asset_id = asset_id
                            new_asset_ids.append(asset_id)
                            derivative_requests.append(
                                _asset_derivative_request(asset_id, path, stat)
                            )
                            existing_source_asset_ids.add(source_asset_id)
                            occupied_ordinals.add(ordinal)

                        work_tag_rows = [
                            {
                                "id": uuid4(),
                                "work_id": work.id,
                                "tag_id": tag["id"],
                                "source": provider.source_name,
                            }
                            for tag in prepared_work["tags"]
                        ]
                        if work_tag_rows:
                            await db.execute(
                                insert(WorkTag)
                                .values(work_tag_rows)
                                .on_conflict_do_nothing(
                                    constraint="uq_work_tags_work_tag"
                                )
                            )
                        work_source_tag_rows = [
                            {
                                "id": uuid4(),
                                "work_source_id": work_source.id,
                                "tag_id": tag["id"],
                                "source": provider.source_name,
                                "original_name": tag.get("original_name"),
                            }
                            for tag in prepared_work["tags"]
                        ]
                        if work_source_tag_rows:
                            await db.execute(
                                insert(WorkSourceTag)
                                .values(work_source_tag_rows)
                                .on_conflict_do_nothing(
                                    constraint="uq_work_source_tags_source_tag"
                                )
                            )

                        changed = bool(changed_fields or new_asset_ids)
                        if changed:
                            await request_media_derivatives(
                                db,
                                derivative_requests,
                            )
                            _creator_dir, lib_dir = (
                                WorkImportService.library_directory(
                                    provider.source_name,
                                    prepared_work["first_raw"],
                                    source_work_id,
                                )
                            )
                            await request_import_projection(
                                db,
                                [
                                    {
                                        "work_id": work.id,
                                        "creator_id": prepared_work[
                                            "linked_creator_id"
                                        ],
                                        "repository_id": download_job.subscription_source_id,
                                        "source": provider.source_name,
                                        "source_work_id": source_work_id,
                                        "metadata_path": str(
                                            (lib_dir / "metadata.json").relative_to(
                                                Path(settings.library_root)
                                            )
                                        ),
                                        "force_metadata_refresh": True,
                                    }
                                ],
                            )
                            await request_search_projection(
                                db,
                                [work.id],
                                creator_ids=(
                                    [prepared_work["linked_creator_id"]]
                                    if prepared_work["linked_creator_id"]
                                    else []
                                ),
                                tag_ids=[tag["id"] for tag in prepared_work["tags"]],
                                repository_ids=(
                                    [download_job.subscription_source_id]
                                    if download_job.subscription_source_id
                                    else []
                                ),
                                subscription_ids=(
                                    [download_job.subscription_id]
                                    if download_job.subscription_id
                                    else []
                                ),
                            )
                            audit_digest = hashlib.sha256(
                                (
                                    f"{download_job.id}:{provider.source_name}:"
                                    f"{source_work_id}"
                                ).encode("utf-8")
                            ).hexdigest()[:32]
                            await db.execute(
                                insert(MaintenanceAuditEvent)
                                .values(
                                    id=uuid4(),
                                    event_type="existing_work_upstream_update",
                                    idempotency_key=(
                                        f"existing-work-update:{audit_digest}"
                                    ),
                                    summary={
                                        "download_job_id": str(download_job.id),
                                        "source": provider.source_name,
                                        "source_work_id": source_work_id,
                                        "work_id": str(work.id),
                                        "changed_fields": changed_fields,
                                        "old_metadata_sha256": old_raw_hash,
                                        "new_metadata_sha256": new_raw_hash,
                                        "asset_ids_added": [
                                            str(value) for value in new_asset_ids
                                        ],
                                    },
                                )
                                .on_conflict_do_nothing(
                                    index_elements=["idempotency_key"]
                                )
                            )
                            result["updated"] += 1
                            result["assets"] += len(new_asset_ids)
                        else:
                            result["unchanged"] += 1
                        if len(existing_assets) + len(new_asset_ids) > 1:
                            result["multi_page"] += 1
                        successful.append(source_work_id)
                except Exception as error:
                    result["failures"][source_work_id] = _safe_work_error(error)
                    logger.warning(
                        "Existing work update failed for %s/%s: %s",
                        provider.source_name,
                        source_work_id,
                        result["failures"][source_work_id],
                        exc_info=True,
                    )

            ledger = ArtifactLedger(db)
            if successful:
                await ledger.mark_works(
                    download_job.id,
                    successful,
                    "done",
                    source=provider.source_name,
                    preserve_active_leases=True,
                )
            for message in set(result["failures"].values()):
                failed_ids = [
                    source_work_id
                    for source_work_id, value in result["failures"].items()
                    if value == message and source_work_id in batch_ids
                ]
                if failed_ids:
                    await ledger.mark_works(
                        download_job.id,
                        failed_ids,
                        "failed",
                        message,
                        source=provider.source_name,
                        preserve_active_leases=True,
                    )
            await db.commit()

    return result


BULK_SQL_PARAMETER_BUDGET = 24_000


class _BulkImportConflict(RuntimeError):
    """A bulk insert omitted at least one required client-generated row."""


def _bulk_row_chunks(rows: list[dict[str, Any]]):
    if not rows:
        return
    column_count = max(len(row) for row in rows)
    row_limit = max(1, BULK_SQL_PARAMETER_BUDGET // max(1, column_count))
    for offset in range(0, len(rows), row_limit):
        yield rows[offset : offset + row_limit]


def _bulk_statement_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for _ in _bulk_row_chunks(rows))


def _bulk_statement_count_for(row_count: int, column_count: int) -> int:
    if row_count <= 0:
        return 0
    row_limit = max(1, BULK_SQL_PARAMETER_BUDGET // max(1, column_count))
    return (row_count + row_limit - 1) // row_limit


def _build_import_work(
    *,
    provider: Any,
    download_job: DownloadJob,
    media_work: dict[str, Any],
) -> dict[str, Any]:
    """Build client-ID rows without attaching them to an ORM session."""

    source_work_id = media_work["source_work_id"]
    prepared_work = media_work["prepared_work"]
    first_raw = prepared_work["first_raw"]
    ws_data = prepared_work["ws_data"]
    posted_at = prepared_work["posted_at"]
    asset_files = media_work["asset_files"]
    file_stats = media_work["file_stats"]
    primary_values = media_work["primary_values"]

    work = Work(
        id=uuid4(),
        title=ws_data.get("title"),
        description=ws_data.get("description"),
        posted_at=posted_at,
        is_ai_generated=_detect_ai_generated(first_raw, provider.source_name),
        is_nsfw=_detect_nsfw(first_raw, provider.source_name),
        is_favorite=False,
    )
    work_source = WorkSource(
        id=uuid4(),
        work_id=work.id,
        source=ws_data["source"],
        source_work_id=source_work_id,
        source_url=ws_data.get("source_url"),
        source_creator_id=ws_data.get("source_creator_id"),
        title=ws_data.get("title"),
        description=ws_data.get("description"),
        posted_at=posted_at,
        raw_metadata=ws_data.get("raw_metadata"),
    )

    asset_rows: list[dict[str, Any]] = []
    asset_source_rows: list[dict[str, Any]] = []
    derivative_requests: list[dict[str, Any]] = []
    for index, path in enumerate(asset_files):
        stat = file_stats[path]
        asset_id = uuid4()
        asset_row = {
            "id": asset_id,
            "file_name": path.name,
            "file_path": str(path.relative_to(settings.download_root)),
            "file_size": stat.st_size,
            "mime_type": get_mime_type(path.suffix),
            "width": None,
            "height": None,
            "duration": None,
            "sha256": None,
            "phash": None,
            "thumb_sm_path": None,
            "thumb_md_path": None,
            "thumb_lg_path": None,
            "derivative_version": None,
            "derivative_source_size": None,
            "derivative_source_mtime_ns": None,
            "phash_version": None,
        }
        if index == 0:
            asset_row.update(primary_values)
            work.thumbnail_asset_id = asset_id
        asset_rows.append(asset_row)
        asset_source_rows.append(
            {
                "id": uuid4(),
                "asset_id": asset_id,
                "work_source_id": work_source.id,
                "source": provider.source_name,
                "source_asset_id": path.stem,
                "source_url": None,
                "raw_metadata": None,
                "ordinal": index,
                "role": _asset_role(path),
            }
        )
        derivative_requests.append(
            _asset_derivative_request(
                asset_id,
                path,
                stat,
                primary_derivative_ready=(
                    index == 0 and bool(primary_values.get("thumb_sm_path"))
                ),
            )
        )

    work_tag_rows = [
        {
            "id": uuid4(),
            "work_id": work.id,
            "tag_id": tag["id"],
            "source": provider.source_name,
        }
        for tag in prepared_work["tags"]
    ]
    work_source_tag_rows = [
        {
            "id": uuid4(),
            "work_source_id": work_source.id,
            "tag_id": tag["id"],
            "source": provider.source_name,
            "original_name": tag.get("original_name"),
        }
        for tag in prepared_work["tags"]
    ]
    curation_rows = (
        [
            {
                "id": uuid4(),
                "work_id": work.id,
                "visibility": "trashed",
                "trashed_at": datetime.now(timezone.utc),
                "reason": "creator archived",
            }
        ]
        if prepared_work["creator_archived"]
        else []
    )
    return {
        **media_work,
        "work": work,
        "work_source": work_source,
        "rows": {
            "works": [
                {
                    "id": work.id,
                    "title": work.title,
                    "description": work.description,
                    "posted_at": work.posted_at,
                    "is_nsfw": work.is_nsfw,
                    "is_ai_generated": work.is_ai_generated,
                    "thumbnail_asset_id": work.thumbnail_asset_id,
                    "is_favorite": False,
                }
            ],
            "work_sources": [
                {
                    "id": work_source.id,
                    "work_id": work.id,
                    "source": work_source.source,
                    "source_work_id": work_source.source_work_id,
                    "source_url": work_source.source_url,
                    "source_creator_id": work_source.source_creator_id,
                    "title": work_source.title,
                    "description": work_source.description,
                    "posted_at": work_source.posted_at,
                    "raw_metadata": work_source.raw_metadata,
                }
            ],
            "assets": asset_rows,
            "asset_sources": asset_source_rows,
            "work_tags": work_tag_rows,
            "work_source_tags": work_source_tag_rows,
            "work_curation_states": curation_rows,
        },
        "derivative_requests": derivative_requests,
        "import_projection": {
            "work_id": work.id,
            "creator_id": prepared_work["linked_creator_id"],
            "repository_id": download_job.subscription_source_id,
            "source": provider.source_name,
            "source_work_id": source_work_id,
        },
    }


_DOMAIN_INSERTS = (
    ("works", Work, "required", None),
    ("work_sources", WorkSource, "required", "uq_work_sources_source_id"),
    ("assets", Asset, "required", None),
    ("asset_sources", AssetSource, "required", None),
    ("work_tags", WorkTag, "ignore", "uq_work_tags_work_tag"),
    (
        "work_source_tags",
        WorkSourceTag,
        "ignore",
        "uq_work_source_tags_source_tag",
    ),
    ("work_curation_states", WorkCurationState, "required", None),
)


def _collect_domain_rows(
    staged_works: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    return [
        row
        for staged in staged_works
        for row in staged["rows"][key]
    ]


async def _execute_bulk_domain(
    db: AsyncSession,
    staged_works: list[dict[str, Any]],
    counters: dict[str, int],
) -> int:
    """Insert one work slice with at most one statement per populated table."""

    statement_count = 0
    for key, model, conflict_policy, constraint in _DOMAIN_INSERTS:
        rows = _collect_domain_rows(staged_works, key)
        for chunk in _bulk_row_chunks(rows):
            statement = insert(model).values(chunk)
            if conflict_policy == "ignore":
                statement = statement.on_conflict_do_nothing(
                    constraint=constraint
                )
                counters["domain_execute_calls"] += 1
                await db.execute(statement)
            else:
                statement = statement.on_conflict_do_nothing(
                    constraint=constraint
                ).returning(model.id)
                counters["domain_execute_calls"] += 1
                result = await db.execute(statement)
                returned = list(result.scalars())
                if len(returned) != len(chunk):
                    raise _BulkImportConflict(
                        f"{model.__tablename__}: inserted "
                        f"{len(returned)} of {len(chunk)} required rows"
                    )
            statement_count += 1
    return statement_count


async def _insert_bulk_domain_with_fallback(
    db: AsyncSession,
    staged_works: list[dict[str, Any]],
    results: dict[str, tuple[str, str | None]],
    counters: dict[str, int],
) -> list[dict[str, Any]]:
    """Use one bulk SAVEPOINT normally; recursively isolate rare conflicts."""

    if not staged_works:
        return []
    counters["savepoints"] += 1
    try:
        async with db.begin_nested():
            statement_count = await _execute_bulk_domain(
                db,
                staged_works,
                counters,
            )
    except (IntegrityError, _BulkImportConflict) as error:
        counters["bulk_conflicts"] += 1
        if len(staged_works) == 1:
            staged_work = staged_works[0]
            source_work_id = staged_work["source_work_id"]
            work_source = staged_work.get("work_source")
            if work_source is not None:
                existing_work_id = (
                    await db.execute(
                        select(WorkSource.work_id)
                        .where(
                            WorkSource.source == work_source.source,
                            WorkSource.source_work_id == work_source.source_work_id,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if existing_work_id is not None:
                    # A concurrent importer won the unique source identity.
                    # Its committed work is authoritative; this retry must not
                    # decode/project it again or report a false failure.
                    results[source_work_id] = ("done", None)
                    counters["concurrent_existing"] = (
                        counters.get("concurrent_existing", 0) + 1
                    )
                    return []
            results[source_work_id] = (
                "failed",
                f"database conflict: {_safe_work_error(error)}",
            )
            return []
        midpoint = len(staged_works) // 2
        left = await _insert_bulk_domain_with_fallback(
            db,
            staged_works[:midpoint],
            results,
            counters,
        )
        right = await _insert_bulk_domain_with_fallback(
            db,
            staged_works[midpoint:],
            results,
            counters,
        )
        return [*left, *right]
    counters["domain_statements"] += statement_count
    return staged_works


def _build_search_projection_rows(
    staged_works: list[dict[str, Any]],
    download_job: DownloadJob,
) -> list[dict[str, Any]]:
    identities: dict[tuple[str, str], None] = {}
    for staged in staged_works:
        identities[(DEFAULT_WORKS_INDEX_UID, str(staged["work"].id))] = None
        creator_id = staged["import_projection"].get("creator_id")
        if creator_id:
            identities[(DEFAULT_CREATORS_INDEX_UID, str(creator_id))] = None
        for tag in staged["prepared_work"]["tags"]:
            identities[(DEFAULT_TAGS_INDEX_UID, str(tag["id"]))] = None
    if staged_works and download_job.subscription_source_id:
        identities[
            (
                DEFAULT_REPOSITORIES_INDEX_UID,
                str(download_job.subscription_source_id),
            )
        ] = None
    if staged_works and download_job.subscription_id:
        identities[
            (
                DEFAULT_SUBSCRIPTIONS_INDEX_UID,
                str(download_job.subscription_id),
            )
        ] = None
    return [
        {
            "id": uuid4(),
            "index_uid": index_uid,
            "entity_id": entity_id,
            "action": "upsert",
            "version": 1,
            "attempts": 0,
        }
        for index_uid, entity_id in identities
    ]


async def _execute_bulk_outboxes(
    db: AsyncSession,
    staged_works: list[dict[str, Any]],
    download_job: DownloadJob,
) -> tuple[int, int]:
    """Persist media, curation and all search entities in three bulk streams."""

    statement_count = 0
    derivative_requests = [
        request
        for staged in staged_works
        for request in staged["derivative_requests"]
    ]
    media_rows = [
        {
            "id": uuid4(),
            "asset_id": request["asset_id"],
            "requested": request["requested"],
            "algorithm_version": MEDIA_ALGORITHM_VERSION,
            "source_size": request.get("source_size"),
            "source_mtime_ns": request.get("source_mtime_ns"),
            "state": "pending",
            "attempts": 0,
        }
        for request in derivative_requests
    ]
    for chunk in _bulk_row_chunks(media_rows):
        media_insert = insert(MediaDerivativeOutbox).values(chunk)
        await db.execute(
            media_insert.on_conflict_do_update(
                index_elements=["asset_id"],
                set_={
                    "requested": MediaDerivativeOutbox.requested.op("||")(
                        media_insert.excluded.requested
                    ),
                    "algorithm_version": media_insert.excluded.algorithm_version,
                    "source_size": media_insert.excluded.source_size,
                    "source_mtime_ns": media_insert.excluded.source_mtime_ns,
                    "state": "pending",
                    "available_at": func.now(),
                    "lease_expires_at": None,
                    "completed_at": None,
                    "last_error": None,
                    "updated_at": func.now(),
                },
            )
        )
        statement_count += 1

    import_rows = [
        {
            "id": uuid4(),
            "work_id": staged["work"].id,
            "creator_id": staged["import_projection"].get("creator_id"),
            "repository_id": staged["import_projection"].get("repository_id"),
            "source": staged["import_projection"].get("source"),
            "source_work_id": staged["source_work_id"],
            "state": "pending",
            "attempts": 0,
            "metadata_path": str(
                (staged["lib_dir"] / "metadata.json").relative_to(
                    Path(settings.library_root)
                )
            ),
            "metadata_state": "pending",
            "metadata_attempts": 0,
        }
        for staged in staged_works
    ]
    for chunk in _bulk_row_chunks(import_rows):
        import_insert = insert(ImportCurationOutbox).values(chunk)
        await db.execute(
            import_insert.on_conflict_do_nothing(index_elements=["work_id"])
        )
        statement_count += 1

    search_rows = _build_search_projection_rows(staged_works, download_job)
    for chunk in _bulk_row_chunks(search_rows):
        search_insert = insert(SearchProjectionOutbox).values(chunk)
        await db.execute(
            search_insert.on_conflict_do_update(
                index_elements=["index_uid", "entity_id"],
                set_={
                    "action": search_insert.excluded.action,
                    "version": SearchProjectionOutbox.version + 1,
                    "attempts": 0,
                    "available_at": func.now(),
                    "lease_until": None,
                    "completed_at": None,
                    "last_error": None,
                    "updated_at": func.now(),
                },
            )
        )
        statement_count += 1
    if staged_works:
        mark_works_generation_pending(db)
    return statement_count, len(search_rows)


def normal_import_batch_statement_bound(
    staged_works: list[dict[str, Any]],
    search_entity_count: int,
) -> int:
    """Return the normal-path SQL bound measured by stage_metrics.

    Two claim statements, one batch media-lease verification, one pre-write
    ownership renewal, SAVEPOINT/RELEASE, and four checkpoint statements are
    fixed. Domain and outbox statements scale only when a single table exceeds
    the conservative asyncpg parameter budget. A batch exceeding 60 seconds
    adds one bounded lease-heartbeat statement per interval.
    """

    domain = sum(
        _bulk_statement_count(_collect_domain_rows(staged_works, key))
        for key, *_ in _DOMAIN_INSERTS
    )
    media_rows = sum(len(staged["derivative_requests"]) for staged in staged_works)
    return (
        10
        + domain
        + _bulk_statement_count_for(media_rows, 8)
        + _bulk_statement_count_for(len(staged_works), 11)
        + _bulk_statement_count_for(search_entity_count, 6)
    )


def _safe_work_error(error: BaseException) -> str:
    text = " ".join(str(error).split())
    return (text or type(error).__name__)[:1000]


async def _claim_import_execution(
    job_uuid: UUID,
) -> tuple[ImportJob, UUID] | None:
    """Atomically claim one runnable ImportJob row for this process."""

    execution_token = uuid4()
    async with async_session() as db:
        result = await db.execute(
            select(ImportJob)
            .where(
                ImportJob.id == job_uuid,
                ImportJob.status.in_(("enqueued", "pending")),
            )
            .with_for_update(skip_locked=True)
        )
        import_job = result.scalar_one_or_none()
        if import_job is None:
            return None
        transition_import_job(import_job, "running")
        import_job.execution_token = execution_token
        import_job.execution_attempt = (import_job.execution_attempt or 0) + 1
        apply_import_progress(
            import_job,
            "importing",
            "Import worker started",
            current=0,
            total=import_job.progress_works_total,
        )
        from app.services.tasks import TaskService

        await TaskService(db).ensure_import_task(import_job)
        await TaskService(db).update_subject(
            "import_job",
            import_job.id,
            status="running",
            progress=import_job.progress_data,
        )
        await db.commit()
        return import_job, execution_token


async def _release_import_execution(
    job_uuid: UUID,
    execution_token: UUID,
) -> set[str]:
    """Release only leases still owned by this concrete execution."""

    from app.services.artifact_ledger import ArtifactLedger

    async with async_session() as db:
        released = await ArtifactLedger(db).release_owned_leases(
            job_uuid,
            execution_token,
        )
        await db.execute(
            update(ImportJob)
            .where(
                ImportJob.id == job_uuid,
                ImportJob.execution_token == execution_token,
            )
            .values(execution_token=None)
        )
        await db.commit()
        return released


async def _owned_import_job(
    db: AsyncSession,
    job_uuid: UUID,
    execution_token: UUID,
) -> ImportJob | None:
    return (
        await db.execute(
            select(ImportJob).where(
                ImportJob.id == job_uuid,
                ImportJob.execution_token == execution_token,
            )
        )
    ).scalar_one_or_none()


async def run_import_job(import_job_id: str):
    job_uuid = UUID(import_job_id)
    claimed_execution = await _claim_import_execution(job_uuid)
    if claimed_execution is None:
        logger.info(
            "Import job %s is locked or no longer runnable; skipping",
            import_job_id,
        )
        return
    import_job, execution_token = claimed_execution
    execution_owner = f"{import_job_id}:{execution_token}"

    # ── Start control listener + heartbeat ──
    listener: ControlListener | None = ControlListener(import_job_id)
    heartbeat: HeartbeatPublisher | None = HeartbeatPublisher(
        import_job_id,
        "import",
        pid=os.getpid(),
    )

    try:
        listener.start()
        heartbeat.start()
        async with async_session() as db:
            r2 = await db.execute(select(DownloadJob).where(
                DownloadJob.id == import_job.download_job_id))
            dj = r2.scalar_one_or_none()
        if not dj:
            raise RuntimeError(
                f"download job {import_job.download_job_id} no longer exists"
            )
        async with async_session() as db:
            dj_repo = DownloadJobRepository(db)
            parent = await dj_repo.get(import_job.download_job_id)
            if parent:
                apply_download_progress(
                    parent,
                    "importing",
                    "Import worker started",
                    import_job_id=import_job_id,
                )
                await db.commit()
        provider = registry.get(dj.source)

        # PostgreSQL is the durable source of truth. The Redis/disk list remains
        # a compatibility fallback for jobs created before the ledger migration.
        from app.services.artifact_ledger import ArtifactLedger, managed_artifact_row
        async with async_session() as ledger_db:
            json_rel_paths = await ArtifactLedger(ledger_db).new_metadata_paths(dj.id)

        all_json_files = sorted(
            Path(settings.download_root) / p for p in json_rel_paths
            if (Path(settings.download_root) / p).exists()
        )

        if not all_json_files:
            from app.services.redis_client import get_redis as _get_redis_fb
            legacy = _consume_import_file_list(_get_redis_fb(), import_job_id)
            if legacy:
                all_json_files = sorted(Path(p) for p in legacy if Path(p).exists())
                logger.info("Import %s: loaded %d files from legacy Redis key",
                           import_job_id, len(all_json_files))

        # Maintenance: the file list is in memory now, so drop this job's disk
        # handoff file (the ledger path never consumes it) and sweep stale
        # orphans left by imports that never ran. Best-effort; never fatal.
        try:
            _il_dir = Path(settings.download_root) / ".import-lists"
            cleanup_import_list(_il_dir, import_job_id)
            _pruned = prune_import_lists(_il_dir, max_age_seconds=86400)
            if _pruned:
                logger.info("Pruned %d stale .import-lists file(s)", _pruned)
        except Exception:
            logger.warning("import-list cleanup failed for %s", import_job_id, exc_info=True)

        # Grouping parses provider JSON and can retain raw metadata.  Perform
        # it only after hard admission; critical mode must not materialize an
        # entire download while waiting for the first database slice.
        _parse_started = monotonic()
        async with _import_resource_slice(
            "import_db",
            execution_owner,
        ):
            groups, invalid_metadata = group_metadata_by_work(
                provider,
                all_json_files,
            )
        for metadata_path in invalid_metadata:
            logger.warning("Failed to extract a work ID from JSON %s", metadata_path)
        _parse_ms = int((monotonic() - _parse_started) * 1000)

        if not groups:
            _empty_msg = "Could not extract work IDs from any JSON"
            async with async_session() as db:
                ij = await _owned_import_job(db, job_uuid, execution_token)
                if ij:
                    apply_import_progress(ij, "failed", _empty_msg)
                    transition_import_job(ij, "failed", _empty_msg)
                    from app.services.tasks import TaskService
                    await TaskService(db).update_subject(
                        "import_job",
                        ij.id,
                        status="failed",
                        progress=ij.progress_data,
                        error=_empty_msg,
                    )
                    # Also fail the parent download_job so an empty parse is not a
                    # silent complete-but-empty (G1).
                    _dj_repo = DownloadJobRepository(db)
                    _parent = await _dj_repo.get(ij.download_job_id)
                    if _parent:
                        await _dj_repo.update_status(_parent, "failed", _empty_msg)
                        apply_download_progress(_parent, "failed", _empty_msg)
                    await db.commit()
            return

        async with async_session() as db:
            ij = await _owned_import_job(db, job_uuid, execution_token)
            if ij is None:
                raise RuntimeError("import execution ownership lost after parse")
            apply_import_progress(
                ij,
                "importing",
                f"Importing {len(groups)} works",
                current=0,
                total=len(groups),
            )
            await db.commit()

        total_groups = len(groups)
        existing_ids = await _existing_work_source_ids(
            provider.source_name,
            list(groups),
        )
        existing_groups = {
            source_work_id: items
            for source_work_id, items in groups.items()
            if source_work_id in existing_ids
        }
        new_groups = {
            source_work_id: items
            for source_work_id, items in groups.items()
            if source_work_id not in existing_ids
        }
        # Provider normalization and its lookup queries are bounded to the
        # same 25-work unit used by the durable ledger claim.  This keeps the
        # prepared object graph O(batch) at each resource slice boundary; the
        # final mapping only holds normalized records for this <=200-work
        # provider request and is drained immediately below.
        prepared: dict[str, dict[str, Any]] = {}
        parse_failures: dict[str, str] = {}
        existing_prepared: dict[str, dict[str, Any]] = {}
        existing_group_items = list(existing_groups.items())
        existing_prepare_offset = 0
        while existing_prepare_offset < len(existing_group_items):
            async with _import_resource_slice(
                "import_db",
                execution_owner,
            ) as prepare_limits:
                allowed = max(
                    1,
                    min(
                        int(prepare_limits.work_units),
                        IMPORT_WORK_BATCH_SIZE,
                        len(existing_group_items) - existing_prepare_offset,
                    ),
                )
                normalized, failures = await _prepare_import_metadata(
                    provider,
                    dj,
                    dict(
                        existing_group_items[
                            existing_prepare_offset : existing_prepare_offset
                            + allowed
                        ]
                    ),
                )
                existing_prepared.update(normalized)
                parse_failures.update(failures)
                existing_prepare_offset += allowed

        existing_result = await _update_existing_work_groups(
            provider,
            dj,
            existing_prepared,
        )
        parse_failures.update(existing_result["failures"])
        new_group_items = list(new_groups.items())
        prepare_offset = 0
        while prepare_offset < len(new_group_items):
            async with _import_resource_slice(
                "import_db",
                execution_owner,
            ) as prepare_limits:
                allowed = max(
                    1,
                    min(
                        int(prepare_limits.work_units),
                        IMPORT_WORK_BATCH_SIZE,
                        len(new_group_items) - prepare_offset,
                    ),
                )
                normalized, failures = await _prepare_import_metadata(
                    provider,
                    dj,
                    dict(
                        new_group_items[
                            prepare_offset : prepare_offset + allowed
                        ]
                    ),
                )
                prepared.update(normalized)
                parse_failures.update(failures)
                prepare_offset += allowed
        if parse_failures:
            async with async_session() as ledger_db:
                for message, failed_ids in {
                    error: [
                        source_work_id
                        for source_work_id, value in parse_failures.items()
                        if value == error
                    ]
                    for error in set(parse_failures.values())
                }.items():
                    await ArtifactLedger(ledger_db).mark_works(
                        dj.id,
                        failed_ids,
                        "failed",
                        message,
                    )
                await ledger_db.commit()

        stats = {
            "works": int(existing_result["updated"]),
            "assets": int(existing_result["assets"]),
            "multi_page": int(existing_result["multi_page"]),
            "skipped": len(parse_failures),
            "existing": int(existing_result["unchanged"]),
        }
        _process_started = monotonic()
        pending_batches = deque(_prepared_work_batches(prepared))
        accounted_existing = set(existing_prepared) - set(
            existing_result["failures"]
        )
        contended_round = 0
        while pending_batches:
            prepared_batch = pending_batches.popleft()
            batch_ids = [source_work_id for source_work_id, _ in prepared_batch]
            prepared_by_id = dict(prepared_batch)
            async with measure_async_stage(
                "import_db_batch",
                works=len(prepared_batch),
            ) as metric:
                metric["durable_commits"] = 0
                if listener.should_stop():
                    metric["works_committed"] = 0
                    metric["assets"] = 0
                    await set_resource_state(
                        execution_owner,
                        "yielded",
                        "control_signal",
                        workload="import_db",
                    )
                    logger.info(
                        "Import %s: control signal received (%s), stopping before batch",
                        execution_owner,
                        listener.command,
                    )
                    break

                # Every media/DB phase acquires its own bounded profile lease;
                # there is deliberately no outer job or batch-wide flock.
                try:
                    if listener.should_stop():
                        metric["works_committed"] = 0
                        metric["assets"] = 0
                        logger.info(
                            "Import %s: control signal received (%s), stopping before claim",
                            import_job_id,
                            listener.command,
                        )
                        break

                    # Claim one metadata representative per work under
                    # FOR UPDATE SKIP LOCKED, then make every artifact for the
                    # selected works importing. Commit before any filesystem or
                    # media work so the row locks are always short lived.
                    async with _import_resource_slice(
                        "import_db",
                        execution_owner,
                    ) as import_limits:
                        prepared_batch, deferred_batch = _take_prepared_work_slice(
                            prepared_batch,
                            import_limits.work_units,
                        )
                        if deferred_batch:
                            # Keep the original stable order.  The next slice
                            # resolves a fresh AIMD budget and may grow again.
                            pending_batches.appendleft(deferred_batch)
                        batch_ids = [
                            source_work_id
                            for source_work_id, _ in prepared_batch
                        ]
                        prepared_by_id = dict(prepared_batch)
                        metric["works"] = len(prepared_batch)
                        metric["budget_work_units"] = import_limits.work_units
                        metric["budget_effective_scale"] = (
                            import_limits.effective_scale
                        )
                        async with async_session() as claim_db:
                            claim = await ArtifactLedger(
                                claim_db
                            ).claim_work_batch(
                                dj.id,
                                job_uuid,
                                lease_token=execution_token,
                                source=provider.source_name,
                                source_work_ids=batch_ids,
                                limit=min(
                                    IMPORT_WORK_BATCH_SIZE,
                                    import_limits.work_units,
                                ),
                                lease_seconds=IMPORT_ARTIFACT_LEASE_SECONDS,
                            )
                            await claim_db.commit()
                    metric["durable_commits"] += 1

                    claimed_ids = list(claim.claimed)
                    metric["works_claimed"] = len(claimed_ids)
                    newly_existing = set(claim.existing) - accounted_existing
                    if newly_existing:
                        accounted_existing.update(newly_existing)
                        stats["existing"] += len(newly_existing)
                        logger.info(
                            "Import %s: %d works committed by another execution",
                            import_job_id,
                            len(newly_existing),
                        )
                    contended_batch = [
                        (source_work_id, prepared_by_id[source_work_id])
                        for source_work_id in claim.contended
                    ]
                    if contended_batch:
                        pending_batches.append(contended_batch)
                        metric["works_contended"] = len(contended_batch)
                    if not claimed_ids:
                        metric["works_committed"] = 0
                        metric["assets"] = 0
                        if contended_batch:
                            async with async_session() as ownership_db:
                                still_owned = await _owned_import_job(
                                    ownership_db,
                                    job_uuid,
                                    execution_token,
                                )
                            if still_owned is None:
                                raise RuntimeError(
                                    "import execution ownership lost while waiting for artifacts"
                                )
                            contended_round += 1
                            delay = min(30.0, 2.0 ** min(contended_round, 5))
                            await set_resource_state(
                                execution_owner,
                                "waiting",
                                "artifact_lease_contended",
                                workload="import_db",
                            )
                            # No SQL transaction, resource lease, or flock is
                            # retained while another execution owns the work.
                            await asyncio.sleep(delay)
                        continue
                    contended_round = 0

                    batch_results: dict[str, tuple[str, str | None]] = {}
                    media_ready: list[dict[str, Any]] = []
                    lost_before_media: set[str] = set()
                    try:
                        # One CAS verifies the whole claimed slice. A lightweight
                        # heartbeat renews the set while resource waits or a long
                        # video derive run, without retaining any SQL transaction
                        # or resource-profile flock itself.
                        async with _artifact_batch_lease_guard(
                            dj.id,
                            claimed_ids,
                            job_uuid,
                            execution_token,
                        ) as batch_lease:
                            for source_work_id in claimed_ids:
                                if not batch_lease.owns(source_work_id):
                                    lost_before_media.add(source_work_id)
                                    continue
                                try:
                                    prepared_work = prepared_by_id[source_work_id]
                                    media_workload = _prepared_media_workload(
                                        prepared_work
                                    )
                                    async with _import_resource_slice(
                                        media_workload,
                                        execution_owner,
                                        source_identity=(
                                            f"{provider.source_name}:{source_work_id}"
                                        ),
                                        wait_for_capacity=False,
                                    ) as media_limits:
                                        # Resource admission may wait; the batch
                                        # heartbeat keeps ownership durable and
                                        # this in-memory check is the per-work
                                        # boundary without another SQL roundtrip.
                                        if not batch_lease.owns(source_work_id):
                                            lost_before_media.add(source_work_id)
                                            continue
                                        media_work = await _prepare_work_media(
                                            provider,
                                            prepared_work,
                                            source_work_id,
                                            derive_primary=media_limits is not None,
                                        )
                                        if batch_lease.owns(source_work_id):
                                            media_ready.append(media_work)
                                        else:
                                            lost_before_media.add(source_work_id)
                                except Exception as error:
                                    if not batch_lease.owns(source_work_id):
                                        lost_before_media.add(source_work_id)
                                        continue
                                    message = _safe_work_error(error)
                                    batch_results[source_work_id] = (
                                        "failed",
                                        message,
                                    )
                                    stats["skipped"] += 1
                                    logger.warning(
                                        "Import %s: media preparation failed for %s: %s",
                                        import_job_id,
                                        source_work_id,
                                        message,
                                    )
                    except _ArtifactLeaseLost:
                        # A database outage cannot be distinguished from an
                        # unsafe ownership window. Let the outer retry/finally
                        # release this execution's leases instead of spinning on
                        # its own still-valid token.
                        raise
                    if lost_before_media:
                        pending_batches.append(
                            [
                                (source_work_id, prepared_by_id[source_work_id])
                                for source_work_id in claimed_ids
                                if source_work_id in lost_before_media
                            ]
                        )
                        metric["lease_lost_before_media"] = len(
                            lost_before_media
                        )

                    built_works: list[dict[str, Any]] = []
                    for media_work in media_ready:
                        source_work_id = media_work["source_work_id"]
                        try:
                            built_works.append(
                                _build_import_work(
                                    provider=provider,
                                    download_job=dj,
                                    media_work=media_work,
                                )
                            )
                        except Exception as error:
                            message = _safe_work_error(error)
                            batch_results[source_work_id] = ("failed", message)
                            stats["skipped"] += 1
                            logger.warning(
                                "Import %s: row preparation failed for %s: %s",
                                import_job_id,
                                source_work_id,
                                message,
                            )

                    staged_works: list[dict[str, Any]] = []
                    bulk_counters = {
                        "savepoints": 0,
                        "bulk_conflicts": 0,
                        "domain_statements": 0,
                        "domain_execute_calls": 0,
                    }
                    outbox_statements = 0
                    search_entity_count = 0
                    if built_works:
                        async with _import_resource_slice(
                            "import_db",
                            execution_owner,
                        ):
                            async with async_session() as batch_db:
                                built_ids = {
                                    staged["source_work_id"]
                                    for staged in built_works
                                }
                                owned_for_db = await ArtifactLedger(
                                    batch_db
                                ).renew_work_leases(
                                    dj.id,
                                    built_ids,
                                    expected_import_job_id=job_uuid,
                                    expected_lease_token=execution_token,
                                    lease_seconds=IMPORT_ARTIFACT_LEASE_SECONDS,
                                )
                                lost_before_db = built_ids - owned_for_db
                                if lost_before_db:
                                    pending_batches.append(
                                        [
                                            (
                                                source_work_id,
                                                prepared_by_id[source_work_id],
                                            )
                                            for source_work_id in sorted(
                                                lost_before_db
                                            )
                                        ]
                                    )
                                    built_works = [
                                        staged
                                        for staged in built_works
                                        if staged["source_work_id"]
                                        in owned_for_db
                                    ]
                                    metric["lease_lost_before_db"] = len(
                                        lost_before_db
                                    )
                                staged_works = (
                                    await _insert_bulk_domain_with_fallback(
                                        batch_db,
                                        built_works,
                                        batch_results,
                                        bulk_counters,
                                    )
                                )
                                rejected_ids = {
                                    staged["source_work_id"]
                                    for staged in built_works
                                } - {
                                    staged["source_work_id"]
                                    for staged in staged_works
                                }
                                concurrent_existing = sum(
                                    1
                                    for source_work_id in rejected_ids
                                    if batch_results.get(source_work_id, (None,))[0]
                                    == "done"
                                )
                                stats["existing"] += concurrent_existing
                                stats["skipped"] += (
                                    len(rejected_ids) - concurrent_existing
                                )
                                if staged_works:
                                    (
                                        outbox_statements,
                                        search_entity_count,
                                    ) = await _execute_bulk_outboxes(
                                        batch_db,
                                        staged_works,
                                        dj,
                                    )
                                    # One durable transaction owns all
                                    # successful domain and outbox rows.
                                    await batch_db.commit()
                                    metric["durable_commits"] += 1
                                else:
                                    await batch_db.rollback()

                    metric["savepoints"] = bulk_counters["savepoints"]
                    metric["bulk_conflicts"] = bulk_counters["bulk_conflicts"]
                    metric["concurrent_existing"] = bulk_counters.get(
                        "concurrent_existing",
                        0,
                    )
                    metric["domain_statements"] = bulk_counters[
                        "domain_statements"
                    ]
                    metric["domain_execute_calls"] = bulk_counters[
                        "domain_execute_calls"
                    ]
                    metric["outbox_statements"] = outbox_statements
                    if staged_works and not bulk_counters["bulk_conflicts"]:
                        metric["normal_sql_bound"] = (
                            normal_import_batch_statement_bound(
                                staged_works,
                                search_entity_count,
                            )
                        )
                    metric["works_committed"] = len(staged_works)
                    metric["assets"] = sum(
                        len(staged["asset_files"]) for staged in staged_works
                    )
                    metric["works_failed"] = sum(
                        1
                        for state, _ in batch_results.values()
                        if state == "failed"
                    )

                    # ``metadata.json`` is now owned exclusively by the
                    # durable ImportCurationOutbox substate committed above.
                    # Keeping the importer off that target closes the DB/file
                    # crash gap and prevents an inline writer racing the
                    # token-fenced projection worker.
                    managed_rows: list[dict[str, Any]] = []
                    for staged in staged_works:
                        source_work_id = staged["source_work_id"]
                        asset_files = staged["asset_files"]
                        lib_dir = staged["lib_dir"]

                        stats["works"] += 1
                        stats["assets"] += len(asset_files)
                        if len(asset_files) > 1:
                            stats["multi_page"] += 1
                        batch_results[source_work_id] = ("done", None)

                        library_paths = [
                            *lib_dir.glob("*.thumbnail.webp"),
                            *lib_dir.glob("*.poster.webp"),
                        ]
                        managed_rows.extend(
                            managed_artifact_row(
                                path,
                                Path(settings.library_root),
                                "library",
                                provider.source_name,
                                staged["creator_dir"],
                                source_work_id,
                                (
                                    "video_poster"
                                    if path.name.endswith(".poster.webp")
                                    else "thumbnail"
                                ),
                            )
                            for path in library_paths
                            if path.exists()
                        )

                        work_dir = staged["work_dir"]
                        try:
                            remaining_assets = [
                                path
                                for path in work_dir.iterdir()
                                if path.is_file()
                                and path.suffix.lower() in ASSET_EXTENSIONS
                            ]
                            if not remaining_assets:
                                for leftover in work_dir.iterdir():
                                    try:
                                        leftover.unlink()
                                    except Exception:
                                        pass
                                try:
                                    work_dir.rmdir()
                                except Exception:
                                    pass
                        except Exception:
                            logger.debug(
                                "Failed to check image files in %s",
                                work_dir,
                                exc_info=True,
                            )

                    # Terminal ledger state and progress share one post-commit
                    # checkpoint transaction. This is one CASE UPDATE for up to
                    # 25 distinct outcomes, not one transaction per work.
                    async with _import_resource_slice(
                        "import_db",
                        execution_owner,
                    ):
                        async with async_session() as checkpoint_db:
                            ledger = ArtifactLedger(checkpoint_db)
                            expected_results = set(batch_results)
                            marked_results = await ledger.mark_work_results(
                                dj.id,
                                batch_results,
                                expected_import_job_id=job_uuid,
                                expected_lease_token=execution_token,
                            )
                            if marked_results != expected_results:
                                missing = sorted(
                                    expected_results - marked_results
                                )
                                raise RuntimeError(
                                    "artifact checkpoint CAS mismatch: "
                                    f"{missing[:5]}"
                                )
                            await ledger.upsert_many(managed_rows)
                            import_progress = await _owned_import_job(
                                checkpoint_db,
                                job_uuid,
                                execution_token,
                            )
                            if import_progress is None:
                                raise RuntimeError(
                                    "import execution ownership lost before progress checkpoint"
                                )
                            completed = min(
                                total_groups,
                                stats["works"]
                                + stats["existing"]
                                + stats["skipped"],
                            )
                            apply_import_progress(
                                import_progress,
                                "importing",
                                (
                                    f"Imported {stats['works']} works; "
                                    f"processed {completed} of {total_groups}"
                                ),
                                current=completed,
                                total=total_groups,
                                assets=stats["assets"],
                            )
                            await checkpoint_db.commit()
                    metric["durable_commits"] += 1
                finally:
                    # Give the adaptive controller an explicit checkpoint
                    # between slices; no DB transaction or flock survives it.
                    await set_resource_state(
                        execution_owner,
                        "yielded",
                        "slice_complete",
                        workload="import_db",
                    )
        # Determine final status based on control signal
        if listener.command == "pause":
            async with async_session() as db:
                ij = await _owned_import_job(db, job_uuid, execution_token)
                if ij:
                    ij.status = "paused"
                    if listener.reason:
                        ij.user_note = listener.reason
                    apply_import_progress(
                        ij,
                        "paused",
                        f"Paused after {stats['works']} works",
                        current=stats["works"],
                        total=total_groups,
                        assets=stats["assets"],
                    )
                    await project_import_pipeline_state(
                        db,
                        ij,
                        status="paused",
                    )
                    await db.commit()
            logger.info("Import %s paused after %d works", import_job_id, stats["works"])
            return
        elif listener.command == "cancel":
            async with async_session() as db:
                ij = await _owned_import_job(db, job_uuid, execution_token)
                if ij:
                    ij.status = "cancelled"
                    if listener.reason:
                        ij.user_note = listener.reason
                    apply_import_progress(
                        ij,
                        "cancelled",
                        f"Cancelled after {stats['works']} works",
                        current=stats["works"],
                        total=total_groups,
                        assets=stats["assets"],
                    )
                    await project_import_pipeline_state(
                        db,
                        ij,
                        status="cancelled",
                    )
                    await db.commit()
            logger.info("Import %s cancelled after %d works", import_job_id, stats["works"])
            return

        # ── Classify outcome (pure, unit-tested) ──
        # Note: total_groups is always >= 1 here — the empty case returned early
        # above ("if not groups"). classify_import_outcome's total_groups==0 rule is
        # a defensive safety net for the pure helper, not the live empty guard.
        _process_ms = int((monotonic() - _process_started) * 1000)
        async with async_session() as _cfg_db:
            _defaults = await get_download_defaults(_cfg_db)
        threshold = clamp_threshold(_defaults.get("import_skip_threshold", 0.5))
        status, message = classify_import_outcome(
            total_groups=total_groups,
            works=stats["works"],
            assets=stats["assets"],
            existing=stats["existing"],
            skipped=stats["skipped"],
            threshold=threshold,
        )

        async with async_session() as db:
            ij = await _owned_import_job(db, job_uuid, execution_token)
            if ij is None:
                raise RuntimeError("import execution ownership lost before completion")
            transition_import_job(ij, status, message if status == "failed" else None)
            apply_import_progress(
                ij, status, message,
                current=stats["works"], total=total_groups, assets=stats["assets"],
            )
            from app.services.tasks import TaskService
            await TaskService(db).update_subject(
                "import_job",
                ij.id,
                status=status,
                progress=ij.progress_data,
                result={"stats": stats, "message": message} if status == "complete" else None,
                error=message if status == "failed" else None,
            )
            if status == "failed":
                logger.warning("Import %s classified failed: %s", import_job_id, message)

            dj_repo = DownloadJobRepository(db)
            dj = await dj_repo.get(ij.download_job_id)
            if dj:
                update_manifest(dj, import_stats=stats)
                append_manifest_event(dj, "import_complete", status=status, **stats)
                append_manifest_event(dj, "stage_timing", stage="parse", ms=_parse_ms)
                append_manifest_event(dj, "stage_timing", stage="process", ms=_process_ms)
                manifest = dj.manifest or {}
                outcome = (
                    build_sync_outcome(
                        "new_content" if stats["works"] > 0 else "no_changes",
                        metadata_count=int(manifest.get("metadata_json_count") or total_groups),
                        media_count=int(manifest.get("image_count") or stats["assets"]),
                    )
                    if status == "complete"
                    else None
                )
                await finalize_download_job(
                    db,
                    dj,
                    status=status,
                    outcome=outcome,
                    error=message if status == "failed" else None,
                    message=message,
                    assets=stats["assets"],
                )
            else:
                await db.commit()

        logger.info("Import finished: %d works, %d assets, %d skipped, %d multi-page (batched)",
                     stats["works"], stats["assets"], stats.get("skipped", 0), stats["multi_page"])

    except Exception as e:
        import traceback
        error_text = f"{str(e)[:1000]}\n{traceback.format_exc()[-500:]}"
        async with async_session() as db:
            ij = await _owned_import_job(db, job_uuid, execution_token)
            if ij:
                # Retry up to max_import_retries times (default 3)
                retry_count = (ij.import_retry_count or 0) + 1
                max_retries = ij.max_import_retries or 3
                if retry_count <= max_retries:
                    ij.import_retry_count = retry_count
                    # Two-step transition: running → failed → enqueued
                    # (state machine does not allow running → enqueued directly)
                    transition_import_job(ij, "failed",
                        f"RETRY {retry_count}/{max_retries}\n{error_text}")
                    transition_import_job(ij, "enqueued",
                        f"RETRY {retry_count}/{max_retries}\n{error_text}")
                    apply_import_progress(
                        ij,
                        "enqueued",
                        f"Retry {retry_count}/{max_retries} queued after import error",
                    )
                    from app.services.tasks import TaskService
                    await TaskService(db).update_subject(
                        "import_job",
                        ij.id,
                        status="enqueued",
                        progress=ij.progress_data,
                        error=f"RETRY {retry_count}/{max_retries}\n{error_text}",
                    )
                    backoff_seconds = 60 * retry_count
                    prepared = await prepare_import_dispatch(
                        db,
                        ij,
                        job_timeout=7200,
                        delay_seconds=backoff_seconds,
                        action="retry",
                    )
                    # The retry intent and deterministic RQ id are committed
                    # before Redis publication.  Capacity/disconnect failures
                    # therefore remain visible to import recovery as enqueued.
                    await db.commit()
                    publication = await publish_prepared_import(
                        db,
                        ij.id,
                        prepared.rq_job_id,
                    )
                    if publication in {"replayed", "existing"}:
                        logger.info("Re-enqueued import job %s for retry %d/%d",
                                   import_job_id, retry_count, max_retries)
                    elif publication == "deferred":
                        logger.warning(
                            "Import retry publication deferred job=%s; durable recovery will retry",
                            import_job_id,
                        )
                    elif publication == "invalid":
                        message = "Import retry publication is invalid; operator action required"
                        raise RuntimeError(message)
                else:
                    transition_import_job(ij, "failed",
                        f"Exhausted {max_retries} retries\n{error_text}")
                    apply_import_progress(
                        ij,
                        "failed",
                        "Import failed after all retries",
                    )
                    await project_import_pipeline_state(
                        db,
                        ij,
                        status="failed",
                        error=f"Exhausted {max_retries} retries\n{error_text}",
                    )
                    await db.commit()
            else:
                logger.warning(
                    "Import execution %s lost ownership; suppressing stale retry",
                    execution_owner,
                )

    finally:
        # Every exit path (including early parse/download failures and retry
        # publication errors) tears down control threads and CAS-releases only
        # the leases owned by this execution token.
        if heartbeat is not None:
            heartbeat.stop()
        if listener is not None:
            listener.stop()
        release_task = asyncio.create_task(
            _release_import_execution(job_uuid, execution_token)
        )
        try:
            released = await asyncio.shield(release_task)
            if released:
                logger.info(
                    "Released %d unfinished artifact work lease(s) for %s",
                    len(released),
                    execution_owner,
                )
        except asyncio.CancelledError:
            # Preserve cancellation semantics, but do not strand an owned
            # artifact lease merely because RQ stopped this coroutine.
            await release_task
            raise
        except Exception:
            logger.error(
                "Failed to release import execution %s",
                execution_owner,
                exc_info=True,
            )

async def cleanup_metadata_jsons(download_root: str = None):
    """Remove orphaned gallery-dl metadata JSON files from downloads directory."""
    import os, logging
    from pathlib import Path
    logger = logging.getLogger(__name__)
    root = Path(download_root or "/downloads")
    removed = 0
    for json_file in root.rglob("*.json"):
        try:
            json_file.unlink()
            removed += 1
        except Exception:
            pass
    logger.info("Cleaned up %d metadata JSON files from %s", removed, root)
    return removed
