"""System info, storage, memory, settings, proxy, integrity check, reset."""

import asyncio
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.integrity import scan_integrity as _run_integrity_check
from app.services.connectivity import probe_proxy_connectivity as _run_proxy_connectivity_test, redact_proxy_url as _redact_proxy_url

logger = logging.getLogger(__name__)

from app.auth import RequirePermission
from app.database import async_session, get_db
from app.models.system_setting import SystemSetting
from app.models.subscription_source import SubscriptionSource
from app.schemas.schedule import CalendarScheduleRule, normalize_legacy_schedule_payload
from app.schemas.gitllery import GitllerySettingsResponse
from app.schemas.data_center import StorageBreakdownResponse, SystemInfoResponse
from app.schemas.admin_operations import (
    AdminOperationAccepted,
    AdminOperationSnapshotResponse,
)
from app.services.redis_client import get_redis
from app.services.queue_admission import (
    QueueAdmissionError,
    checked_enqueue,
    checked_enqueue_in,
)
from app.services import admin_data
from app.services.admin_data import ENTITIES, clear_entity_data
from app.services.subscription_replan import (
    replan_inherited_subscription_sources,
    subscription_schedule_changed,
)

from ._routers import router


def _job_is_sync_scan(job) -> bool:
    return "sync_subscriptions" in (getattr(job, "func_name", "") or str(job))


def _reschedule_subscription_sync_scan(config: dict) -> dict:
    """Replace pending subscription sync scans with the current interval."""
    from rq import Queue
    from rq.registry import ScheduledJobRegistry

    from app.jobs.subscription_sync import sync_subscriptions

    interval = max(int(config.get("scheduler_scan_interval_minutes", 60)), 5)
    redis = get_redis()
    queue = Queue(name="scheduled", connection=redis)
    scheduled_registry = ScheduledJobRegistry(queue=queue)

    scheduled_old_ids = []
    for job_id in list(scheduled_registry.get_job_ids()):
        job = queue.fetch_job(job_id)
        if job and _job_is_sync_scan(job):
            scheduled_old_ids.append(job_id)

    queued_old_ids = []
    for job in list(queue.get_jobs()):
        if _job_is_sync_scan(job):
            queued_old_ids.append(job.id)

    # Publish first so a late Redis capacity/write failure never deletes the
    # only known-good recurring scan. The replacement is at least five minutes
    # away, leaving time to remove the superseded entries below.
    job = checked_enqueue_in(
        queue,
        timedelta(minutes=interval),
        sync_subscriptions,
    )
    removed = 0
    for old_job_id in scheduled_old_ids:
        if old_job_id != job.id:
            scheduled_registry.remove(old_job_id, delete_job=True)
            removed += 1
    for old_job_id in queued_old_ids:
        if old_job_id != job.id:
            queue.remove(old_job_id)
            removed += 1
    return {"removed": removed, "job_id": job.id, "interval_minutes": interval}


async def _enqueue_download_conflict_reconciliation() -> dict:
    from app.services.operations import enqueue_admin_operation

    return await enqueue_admin_operation(
        lock_key="diagnostics:download-conflicts:active",
        operation_type="admin-download-conflict-reconciliation",
        title="Reconcile historical download conflicts",
        entity="download-conflicts",
        func="app.jobs.download_conflicts.reconcile_historical_download_conflicts",
        options={"limit": 500},
        job_timeout=3600,
        queue_name="maintenance",
    )


DEFAULT_DEDUP = {
    "auto_group_enabled": True,
    "phash_threshold": 4,
    "ssim_threshold": 0.98,
    "aspect_ratio_tolerance": 0.01,
    "auto_group_score": 95,
    "review_score": 70,
    "quarantine_days": 30,
}
DEFAULT_DL = {"timeout_seconds": 600, "max_retries": 3, "retry_backoff_base_seconds": 60, "max_posts": 200, "skip_ai_generated": False, "auto_resolve_upstream_conflicts": True}

_system_info_cache: dict | None = None
_system_info_cache_ts: float = 0.0
_SYSTEM_INFO_CACHE_TTL = 60.0
_system_info_lock = asyncio.Lock()

class DedupSettings(BaseModel):
    auto_group_enabled: bool = True
    phash_threshold: int = Field(default=4, ge=0, le=4)
    ssim_threshold: float = Field(default=0.98, ge=0.9, le=1.0)
    aspect_ratio_tolerance: float = Field(default=0.01, ge=0.0, le=0.05)
    auto_group_score: float = Field(default=95, ge=70, le=100)
    review_score: float = Field(default=70, ge=0, le=100)
    quarantine_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def validate_score_order(self):
        if self.review_score > self.auto_group_score:
            raise ValueError("review_score must not exceed auto_group_score")
        return self

class SubscriptionDefaults(BaseModel):
    default_sync_interval_hours: int = 6
    scheduler_scan_interval_minutes: int = 60
    scheduler_enabled: bool = True
    schedule_mode: Literal["interval", "calendar"] = "interval"
    schedule_rule: CalendarScheduleRule | None = None
    scheduled_times: str = ""
    timezone: str = "UTC"
    auto_enable_sources: str = "pixiv"

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_schedule(cls, value):
        return normalize_legacy_schedule_payload(value)

    @model_validator(mode="after")
    def require_calendar_rule(self):
        if self.schedule_mode == "calendar" and self.schedule_rule is None:
            raise ValueError("calendar schedule_mode requires schedule_rule")
        return self

DEFAULT_SUB = {"default_sync_interval_hours": 6, "scheduler_scan_interval_minutes": 60, "scheduler_enabled": True, "schedule_mode": "interval", "scheduled_times": "", "timezone": "UTC", "auto_enable_sources": "pixiv"}

class DownloadDefaults(BaseModel):
    # extra="allow" preserves fields the frontend sends that aren't declared here
    # (stall_timeout_seconds, gallerydl_*, import_skip_threshold). Without it,
    # saving this page silently dropped those keys back to their defaults.
    model_config = {"extra": "allow"}
    timeout_seconds: int = 600
    max_retries: int = 3
    retry_backoff_base_seconds: int = 60
    max_posts: int = 200
    skip_ai_generated: bool = False
    download_concurrency: int = 3  # parallel download jobs, clamped to 1-5 on read
    auto_resolve_upstream_conflicts: bool = True

class ProxySettings(BaseModel):
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = "localhost,127.0.0.1,::1"
    enabled: bool = False

DEFAULT_PROXY = {"http_proxy": "", "https_proxy": "", "no_proxy": "localhost,127.0.0.1,::1", "enabled": False}

class AdminSettingsUpdate(BaseModel):
    dedup: DedupSettings | None = None
    subscription_defaults: SubscriptionDefaults | None = None
    download_defaults: DownloadDefaults | None = None
    proxy: ProxySettings | None = None

async def _get_setting(db: AsyncSession, key: str, default: dict = None) -> dict:
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = result.scalar_one_or_none()
    if row and isinstance(row.value, dict):
        return {**(default or {}), **row.value}
    return default or {}


async def _put_setting(db: AsyncSession, key: str, value: dict):
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    row = result.scalar_one_or_none()
    previous = dict(row.value) if row and isinstance(row.value, dict) else {}
    changed = row is None or previous != value
    if row:
        row.value = value
    else:
        db.add(SystemSetting(key=key, value=value))
    if (
        key == "subscription_defaults"
        and changed
        and subscription_schedule_changed(previous, value)
    ):
        await replan_inherited_subscription_sources(db, value)
    await db.commit()

    # Invalidate caches when relevant settings change
    if key == "proxy":
        try:
            from app.services.proxy import clear_proxy_cache
            clear_proxy_cache()
        except Exception:
            pass

def _disk_capacity(path: str) -> dict[str, float]:
    """Read mount capacity without walking the managed storage tree."""
    try:
        usage = shutil.disk_usage(path)
        return {
            "total_gb": round(usage.total / (1024 ** 3), 1),
            "used_gb": round(usage.used / (1024 ** 3), 1),
            "free_gb": round(usage.free / (1024 ** 3), 1),
        }
    except OSError:
        return {"total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0}


async def _ledger_inventory(db: AsyncSession) -> dict:
    """Return bounded storage and entity facts from the artifact ledger."""
    from app.models.asset import Asset
    from app.models.creator import Creator
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.models.tag import Tag
    from app.models.work import Work
    from app.services.artifact_ledger import downloads_artifact_predicate

    bytes_expr = func.coalesce(StorageArtifact.file_size, 0)
    roots_result = await db.execute(
        select(
            StorageArtifact.storage_root,
            func.coalesce(func.sum(bytes_expr), 0).label("size_bytes"),
            func.coalesce(
                func.sum(case((StorageArtifact.artifact_type == "archive", bytes_expr), else_=0)),
                0,
            ).label("archive_bytes"),
        ).group_by(StorageArtifact.storage_root)
    )
    roots = {
        row.storage_root: {
            "size_bytes": int(row.size_bytes or 0),
            "archive_bytes": int(row.archive_bytes or 0),
        }
        for row in roots_result
    }

    source_rows = list((await db.execute(
        select(
            StorageArtifact.source,
            StorageArtifact.creator_dir,
            func.coalesce(func.sum(bytes_expr), 0).label("size_bytes"),
            func.count(func.distinct(StorageArtifact.source_work_id)).label("work_count"),
        )
        .where(StorageArtifact.storage_root == "downloads")
        .group_by(StorageArtifact.source, StorageArtifact.creator_dir)
    )).all())
    archive_rows = await db.execute(
        select(
            StorageArtifact.source,
            func.coalesce(func.sum(bytes_expr), 0).label("size_bytes"),
        )
        .where(
            StorageArtifact.storage_root == "downloads",
            StorageArtifact.artifact_type == "archive",
        )
        .group_by(StorageArtifact.source)
    )

    db_counts = (await db.execute(select(
        select(func.count(Work.id)).scalar_subquery().label("works"),
        select(func.count(Asset.id)).scalar_subquery().label("assets"),
        select(func.count(Creator.id)).scalar_subquery().label("creators"),
        select(func.count(Subscription.id)).scalar_subquery().label("subscriptions"),
        select(func.count(Tag.id)).scalar_subquery().label("tags"),
    ))).one()

    pending_states = ("new", "importing")
    pending_works = (
        select(StorageArtifact.source, StorageArtifact.source_work_id)
        .where(
            downloads_artifact_predicate(),
            StorageArtifact.artifact_type == "metadata_json",
            StorageArtifact.state.in_(pending_states),
        )
        .group_by(StorageArtifact.source, StorageArtifact.source_work_id)
        .subquery()
    )
    pipeline = (await db.execute(select(
        select(func.count()).select_from(pending_works).scalar_subquery().label("pending_import_works"),
        select(func.count(StorageArtifact.id))
        .where(
            downloads_artifact_predicate(),
            StorageArtifact.state.in_(pending_states),
            StorageArtifact.download_job_id.is_(None),
        )
        .scalar_subquery()
        .label("orphan_pending_artifacts"),
        select(func.count(StorageArtifact.id))
        .where(
            downloads_artifact_predicate(),
            StorageArtifact.state == "failed",
        )
        .scalar_subquery()
        .label("failed_artifacts"),
    ))).one()
    inventory_updated_at = (await db.execute(select(func.max(StorageArtifact.updated_at)))).scalar_one()

    return {
        "roots": roots,
        "source_rows": source_rows,
        "archives_kb": {
            row.source: round(int(row.size_bytes or 0) / 1024, 1)
            for row in archive_rows
        },
        "db_stats": {
            "works": int(db_counts.works or 0),
            "assets": int(db_counts.assets or 0),
            "creators": int(db_counts.creators or 0),
            "subscriptions": int(db_counts.subscriptions or 0),
            "tags": int(db_counts.tags or 0),
        },
        "inventory_updated_at": inventory_updated_at.isoformat() if inventory_updated_at else None,
        "inventory_source": "storage_artifacts",
        "pipeline_stats": {
            "pending_import_works": int(pipeline.pending_import_works or 0),
            "orphan_pending_artifacts": int(pipeline.orphan_pending_artifacts or 0),
            "failed_artifacts": int(pipeline.failed_artifacts or 0),
        },
    }


@router.get("/system-info", response_model=SystemInfoResponse)
async def system_info(db: AsyncSession = Depends(get_db)):
    """Return ledger-backed storage facts and constant-time mount capacity."""
    global _system_info_cache, _system_info_cache_ts
    now_mono = time.monotonic()
    if _system_info_cache is not None and (now_mono - _system_info_cache_ts) < _SYSTEM_INFO_CACHE_TTL:
        return _system_info_cache
    async with _system_info_lock:
        now_mono = time.monotonic()
        if _system_info_cache is not None and (now_mono - _system_info_cache_ts) < _SYSTEM_INFO_CACHE_TTL:
            return _system_info_cache
        inventory = await _ledger_inventory(db)
        downloads_capacity = await asyncio.to_thread(_disk_capacity, settings.download_root)
        library_capacity = await asyncio.to_thread(_disk_capacity, settings.library_root)
        info = {
            "version": "0.1.0",
            "python": "3.12",
            "downloads_size_mb": round(inventory["roots"].get("downloads", {}).get("size_bytes", 0) / (1024 ** 2), 1),
            "library_size_mb": round(inventory["roots"].get("library", {}).get("size_bytes", 0) / (1024 ** 2), 1),
            "downloads_total_gb": downloads_capacity["total_gb"],
            "downloads_used_gb": downloads_capacity["used_gb"],
            "downloads_free_gb": downloads_capacity["free_gb"],
            "library_total_gb": library_capacity["total_gb"],
            "library_used_gb": library_capacity["used_gb"],
            "library_free_gb": library_capacity["free_gb"],
            "archives_kb": inventory["archives_kb"],
            "db_stats": inventory["db_stats"],
            "inventory_updated_at": inventory["inventory_updated_at"],
            "inventory_source": inventory["inventory_source"],
            "pipeline_stats": inventory["pipeline_stats"],
        }
        _system_info_cache = info
        _system_info_cache_ts = time.monotonic()
        return info


def _current_rss_mb() -> float | None:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)  # kB -> MB
    except Exception:
        pass
    return None


@router.get("/memory")
async def memory_diagnostics(top: int = 25):
    """Return bounded Linux process and SQLAlchemy pool metrics."""
    del top  # Kept as a rolling compatibility query parameter.

    proc: dict[str, int | float] = {}
    try:
        with open("/proc/self/status", encoding="utf-8") as status_file:
            for line in status_file:
                name, separator, raw = line.partition(":")
                if not separator or name not in {
                    "VmRSS",
                    "VmSize",
                    "RssAnon",
                    "RssFile",
                    "Threads",
                }:
                    continue
                value = raw.strip().split()[0]
                if name == "Threads":
                    proc["threads"] = int(value)
                else:
                    proc[
                        {
                            "VmRSS": "rss_mb",
                            "VmSize": "virtual_mb",
                            "RssAnon": "anonymous_rss_mb",
                            "RssFile": "file_rss_mb",
                        }[name]
                    ] = round(int(value) / 1024, 1)
    except (OSError, ValueError, IndexError):
        proc = {}

    from app.database import engine

    pool = engine.sync_engine.pool

    def pool_value(name: str):
        value = getattr(pool, name, None)
        if not callable(value):
            return None
        try:
            return int(value())
        except Exception:
            return None

    runtime_size = pool_value("size")
    return {
        "source": "/proc/self/status",
        "rss_mb": proc.get("rss_mb", _current_rss_mb()),
        "proc": proc,
        "pool": {
            "size": runtime_size if runtime_size is not None else settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "checked_in": pool_value("checkedin"),
            "checked_out": pool_value("checkedout"),
            "overflow": pool_value("overflow"),
        },
    }


from app.services.storage_breakdown_cache import (
    cached_storage_breakdown,
    cache_storage_breakdown,
    invalidate_storage_breakdown_cache,
)


async def _ledger_storage_breakdown(db: AsyncSession) -> dict:
    """Build the Data Center hierarchy from durable artifact rows only."""
    from app.models.creator import Creator
    from app.models.source_creator import SourceCreator
    from app.models.storage_artifact import StorageArtifact
    from app.models.subscription import Subscription
    from app.models.subscription_source import SubscriptionSource
    from app.models.work_source import WorkSource
    from app.providers import registry
    from app.services.settings import extractor_key_for_source

    inventory = await _ledger_inventory(db)
    repository_contexts = list((await db.execute(
        select(SubscriptionSource, Creator)
        .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
        .join(Creator, Creator.id == Subscription.creator_id)
    )).all())
    source_creators = list((await db.execute(
        select(SourceCreator, Creator)
        .outerjoin(Creator, Creator.id == SourceCreator.creator_id)
    )).all())
    work_source_identities = list((await db.execute(
        select(
            StorageArtifact.source,
            StorageArtifact.creator_dir,
            WorkSource.source_creator_id,
        )
        .join(
            WorkSource,
            and_(
                WorkSource.source == StorageArtifact.source,
                WorkSource.source_work_id == StorageArtifact.source_work_id,
            ),
        )
        .where(
            StorageArtifact.storage_root == "downloads",
            WorkSource.source_creator_id.is_not(None),
        )
        .group_by(
            StorageArtifact.source,
            StorageArtifact.creator_dir,
            WorkSource.source_creator_id,
        )
    )).all())

    creators_by_id: dict[str, Creator] = {}
    for repository, creator in repository_contexts:
        creators_by_id[str(creator.id)] = creator

    def source_display_name(source: str) -> str:
        try:
            return registry.get(source).display_name
        except KeyError:
            return source

    def provider_directory(source: str, source_url: str | None) -> str | None:
        if not source_url:
            return None
        try:
            provider = registry.get(source)
            normalized_url = provider.normalize_url(source_url) or source_url
            return provider.get_creator_dir_from_url(normalized_url)
        except KeyError:
            return None

    missing = object()
    ambiguous = object()

    def add_unique(mapping: dict, key: tuple[str, str] | None, value, identity) -> None:
        if key is None:
            return
        existing = mapping.get(key, missing)
        if existing is missing:
            mapping[key] = (identity, value)
        elif existing is not ambiguous and existing[0] != identity:
            mapping[key] = ambiguous

    owner_by_directory: dict[tuple[str, str], object] = {}
    for source_creator, creator in source_creators:
        if source_creator.creator_id:
            owner_id = str(source_creator.creator_id)
            add_unique(
                owner_by_directory,
                (source_creator.source, source_creator.source_creator_id),
                owner_id,
                owner_id,
            )
            url_directory = provider_directory(source_creator.source, source_creator.source_url)
            if url_directory:
                add_unique(
                    owner_by_directory,
                    (source_creator.source, url_directory),
                    owner_id,
                    owner_id,
                )
            if creator:
                creators_by_id[str(creator.id)] = creator

    work_identity_by_directory: dict[tuple[str, str], object] = {}
    for source, directory, source_creator_id in work_source_identities:
        add_unique(
            work_identity_by_directory,
            (source, directory),
            source_creator_id,
            source_creator_id,
        )

    repositories_by_source_creator: dict[tuple[str, str], object] = {}
    repositories_by_provider_directory: dict[tuple[str, str], object] = {}
    for repository, creator in repository_contexts:
        context = (repository, creator)
        if repository.source_creator_id:
            add_unique(
                repositories_by_source_creator,
                (repository.source, repository.source_creator_id),
                context,
                repository.id,
            )
        repository_directory = provider_directory(repository.source, repository.source_url)
        if repository_directory:
            add_unique(
                repositories_by_provider_directory,
                (repository.source, repository_directory),
                context,
                repository.id,
            )

    def unique_repository_context(source: str, directory: str):
        key = (source, directory)
        exact = repositories_by_source_creator.get(key, missing)
        provider_match = repositories_by_provider_directory.get(key, missing)
        if exact is ambiguous or provider_match is ambiguous:
            return None
        matches = [
            value
            for value in (exact, provider_match)
            if value is not missing
        ]
        if not matches:
            return None
        repository_ids = {value[0] for value in matches}
        return matches[0][1] if len(repository_ids) == 1 else None

    source_totals: dict[str, dict] = {}
    creator_nodes: dict[str, dict] = {}
    unlinked_repositories: list[dict] = []
    legacy_creators: list[dict] = []

    for row in inventory["source_rows"]:
        source = row.source
        directory_name = row.creator_dir
        size_bytes = int(row.size_bytes or 0)
        work_count = int(row.work_count or 0)
        source_total = source_totals.setdefault(source, {
            "size_bytes": 0,
            "creator_count": 0,
            "work_count": 0,
        })
        source_total["size_bytes"] += size_bytes
        source_total["creator_count"] += 1
        source_total["work_count"] += work_count

        work_identity_match = work_identity_by_directory.get(
            (source, directory_name),
            missing,
        )
        work_identity = (
            work_identity_match[1]
            if work_identity_match is not missing
            and work_identity_match is not ambiguous
            else None
        )
        identity_keys: list[str] = []
        if work_identity_match is not ambiguous:
            identity_keys.append(directory_name)
            if work_identity and work_identity != directory_name:
                identity_keys.append(work_identity)

        owner_ids: set[str] = set()
        repository_context_by_id: dict[str, tuple] = {}
        for identity_key in identity_keys:
            owner_match = owner_by_directory.get((source, identity_key), missing)
            if owner_match is not missing and owner_match is not ambiguous:
                owner_ids.add(owner_match[1])
            repository_context = unique_repository_context(source, identity_key)
            if repository_context:
                repository, creator = repository_context
                owner_ids.add(str(creator.id))
                repository_context_by_id[str(repository.id)] = repository_context

        creator_id = next(iter(owner_ids)) if len(owner_ids) == 1 else None
        repository_id: str | None = None
        display_name = directory_name
        if creator_id and len(repository_context_by_id) == 1:
            repository, creator = next(iter(repository_context_by_id.values()))
            if str(creator.id) == creator_id:
                repository_id = str(repository.id)
                display_name = creator.display_name or creator.name or display_name
        if not repository_id and creator_id and creator_id in creators_by_id:
            creator = creators_by_id[creator_id]
            display_name = creator.display_name or creator.name or display_name

        size_mb = round(size_bytes / (1024 ** 2), 1)
        child = {
            "repository_id": repository_id,
            "source": source,
            "source_display_name": source_display_name(source),
            "disk_source": extractor_key_for_source(source),
            "directory_name": directory_name,
            "size_mb": size_mb,
            "logical_size_mb": size_mb,
            "work_count": work_count,
        }
        legacy_entry = {
            "name": directory_name,
            "display_name": display_name,
            "source": source,
            "size_mb": size_mb,
            "work_count": work_count,
        }
        if creator_id:
            legacy_entry["creator_id"] = creator_id
        if repository_id:
            legacy_entry["repository_id"] = repository_id
        legacy_creators.append(legacy_entry)

        if not creator_id:
            unlinked_repositories.append(child)
            continue
        node = creator_nodes.setdefault(creator_id, {
            "creator_id": creator_id,
            "display_name": display_name,
            "size_mb": 0.0,
            "work_count": 0,
            "repository_count": 0,
            "repositories": [],
        })
        node["size_mb"] = round(node["size_mb"] + size_mb, 1)
        node["work_count"] += work_count
        node["repository_count"] += 1
        node["repositories"].append(child)

    creator_tree = sorted(
        creator_nodes.values(),
        key=lambda node: (-node["size_mb"], node["display_name"].casefold()),
    )[:20]
    for node in creator_tree:
        node["repositories"].sort(
            key=lambda child: (-child["size_mb"], child["source"], child["directory_name"]),
        )
    unlinked_repositories.sort(
        key=lambda child: (-child["size_mb"], child["source"], child["directory_name"]),
    )

    return {
        "sources": {
            source: {
                "size_mb": round(stats["size_bytes"] / (1024 ** 2), 1),
                "logical_size_mb": round(stats["size_bytes"] / (1024 ** 2), 1),
                "creator_count": stats["creator_count"],
                "work_count": stats["work_count"],
            }
            for source, stats in source_totals.items()
        },
        "creators": sorted(
            legacy_creators,
            key=lambda entry: (-entry["size_mb"], entry["display_name"].casefold()),
        )[:20],
        "creator_tree": creator_tree,
        "unlinked_repositories": unlinked_repositories,
        "db_stats": inventory["db_stats"],
        "inventory_updated_at": inventory["inventory_updated_at"],
        "inventory_source": inventory["inventory_source"],
        "pipeline_stats": inventory["pipeline_stats"],
        "layers": {
            "original_media_store": {
                "path": settings.download_root,
                "size_mb": round(inventory["roots"].get("downloads", {}).get("size_bytes", 0) / (1024 ** 2), 1),
                "description": "Ledger-tracked artifacts in DOWNLOAD_ROOT.",
            },
            "library_index": {
                "path": settings.library_root,
                "size_mb": round(inventory["roots"].get("library", {}).get("size_bytes", 0) / (1024 ** 2), 1),
                "description": "Ledger-tracked metadata and thumbnails in LIBRARY_ROOT.",
            },
            "download_archives": {
                "path": settings.download_root,
                "size_mb": round(inventory["roots"].get("downloads", {}).get("archive_bytes", 0) / (1024 ** 2), 1),
                "description": "Ledger-tracked download archive files.",
            },
            "backups": {
                "path": f"{Path(settings.download_root) / '.backups'}; {Path(settings.app_config_root) / 'backups'}",
                "size_mb": round(inventory["roots"].get("backups", {}).get("size_bytes", 0) / (1024 ** 2), 1),
                "description": "Ledger-tracked backup artifacts.",
            },
        },
    }


@router.get("/storage-breakdown", response_model=StorageBreakdownResponse, response_model_exclude_unset=True)
async def storage_breakdown(db: AsyncSession = Depends(get_db)):
    """Return a bounded, ledger-backed storage breakdown."""
    cached = cached_storage_breakdown()
    if cached is not None:
        return cached
    result = await _ledger_storage_breakdown(db)
    cache_storage_breakdown(result)
    return result


@router.post(
    "/integrity-check",
    status_code=202,
    response_model=AdminOperationAccepted,
)
async def integrity_check():
    """Start a durable integrity scan without walking storage in this request."""
    from app.services.operations import start_admin_operation

    return await start_admin_operation(
        operation_type="admin-integrity-scan",
        scope_key="diagnostics:integrity:active",
        title="Integrity scan",
        entity="integrity",
        options={},
        queue_name="maintenance",
    )


@router.get(
    "/integrity-check/latest",
    response_model=AdminOperationSnapshotResponse,
)
async def latest_integrity_check(db: AsyncSession = Depends(get_db)):
    """Read the latest successful integrity result from PostgreSQL."""
    from app.services.operations import latest_successful_admin_operation

    return await latest_successful_admin_operation(
        db,
        operation_type="admin-integrity-scan",
        scope_key="diagnostics:integrity:active",
        include_retryable=True,
    )




@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    dedup = await _get_setting(db, "dedup", DEFAULT_DEDUP)
    sub = await _get_setting(db, "subscription_defaults", DEFAULT_SUB)
    dl = await _get_setting(db, "download_defaults", DEFAULT_DL)
    proxy = await _get_setting(db, "proxy", DEFAULT_PROXY)
    return {"dedup": dedup, "subscription_defaults": sub, "download_defaults": dl, "proxy": proxy}


@router.get("/gitllery/settings", response_model=GitllerySettingsResponse)
async def get_gitllery_settings(db: AsyncSession = Depends(get_db)):
    """Return Gitllery v1's deployment-managed, read-only control surface.

    The parent admin router requires the ``system`` permission.  Keep this
    response deliberately declarative: it exposes no environment values,
    filesystem paths, credentials, or mutation controls.
    """

    from app.services.gitllery import GitlleryService

    mode = settings.gitllery_projection_mode.strip().lower()
    projection_mode: Literal["shadow", "active"] = (
        "active" if mode == "active" else "shadow"
    )
    projection_reason = None if projection_mode == "active" else "gitllery_shadow_only"
    projection_capability = {
        "enabled": projection_mode == "active",
        "reason": projection_reason,
    }
    transfer_capability = {
        "enabled": False,
        "reason": (
            "gitllery_shadow_only"
            if projection_mode == "shadow"
            else "gitllery_transfer_not_implemented"
        ),
    }
    status = await GitlleryService(db).status(deep=False)
    return {
        "product_name": "Gitllery",
        "product_version": "v1",
        "format_id": "gitllery-segment",
        "format_revision": 1,
        "projection_mode": projection_mode,
        "build_generation": settings.gitllery_build_generation,
        "managed_by": "deployment_environment",
        "read_only": True,
        "capabilities": {
            "automatic_projection": projection_capability,
            "reconcile": projection_capability,
            "backfill": projection_capability,
            "rebuild": projection_capability,
            "push": transfer_capability,
            "pull": transfer_capability,
            "verify": {"enabled": True, "reason": None},
            "commit": {"enabled": True, "reason": None},
        },
        "cli": {
            "max_works_per_commit": 25,
            "max_operations_per_commit": 100,
            "token_storage": "client_only",
            "server_stores_cli_token": False,
            "examples": {
                "config": "gitllery config set url http://auto-gallery.local",
                "login": "gitllery auth login --username admin",
                "status": "gitllery --remote status",
                "log": "gitllery --remote log --limit 50",
                "verify": "gitllery verify --remote",
                "commit": (
                    "gitllery --remote commit --message \"curate work\" "
                    "work favorite 00000000-0000-0000-0000-000000000001 --set on"
                ),
            },
        },
        "governance_scope": {
            "observation": "host_and_auto_gallery",
            "enforcement": "auto_gallery_only",
            "modifies_other_projects": False,
            "modifies_host_configuration": False,
        },
        "status": status,
    }


@router.put("/settings")
async def update_settings(data: AdminSettingsUpdate, db: AsyncSession = Depends(get_db)):
    sync_scan_reschedule = None
    conflict_reconciliation = None
    if data.dedup is not None:
        await _put_setting(db, "dedup", data.dedup.model_dump())
    if data.subscription_defaults is not None:
        subscription_defaults = data.subscription_defaults.model_dump()
        await _put_setting(db, "subscription_defaults", subscription_defaults)
        try:
            sync_scan_reschedule = _reschedule_subscription_sync_scan(subscription_defaults)
        except QueueAdmissionError:
            raise
        except Exception:
            logger.warning("Failed to reschedule subscription sync scan after settings update", exc_info=True)
    if data.download_defaults is not None:
        download_defaults = data.download_defaults.model_dump()
        await _put_setting(db, "download_defaults", download_defaults)
        if download_defaults.get("auto_resolve_upstream_conflicts", True):
            try:
                conflict_reconciliation = await _enqueue_download_conflict_reconciliation()
            except Exception:
                logger.warning("Failed to enqueue historical conflict reconciliation", exc_info=True)
    if data.proxy is not None:
        await _put_setting(db, "proxy", data.proxy.model_dump())
    return {
        "status": "ok",
        "message": "Settings saved to database",
        "sync_scan_reschedule": sync_scan_reschedule,
        "conflict_reconciliation": conflict_reconciliation,
    }


# ── Proxy Test ──

@router.post(
    "/proxy/test",
    status_code=202,
    response_model=AdminOperationAccepted,
)
async def test_proxy_connectivity():
    """Start the proxy connectivity test as a durable TaskRun."""
    from app.services.operations import start_admin_operation

    return await start_admin_operation(
        operation_type="admin-proxy-test",
        scope_key="diagnostics:proxy:active",
        title="Proxy connectivity test",
        entity="proxy-test",
        options={},
        queue_name="maintenance",
    )


@router.get(
    "/proxy/test/latest",
    response_model=AdminOperationSnapshotResponse,
)
async def latest_proxy_connectivity(db: AsyncSession = Depends(get_db)):
    """Read the latest successful proxy connectivity result."""
    from app.services.operations import latest_successful_admin_operation

    return await latest_successful_admin_operation(
        db,
        operation_type="admin-proxy-test",
        scope_key="diagnostics:proxy:active",
    )






# ── Data Management ──

@router.post("/reset-settings")
async def reset_settings(db: AsyncSession = Depends(get_db)):
    """Reset all system settings to defaults."""
    return await clear_entity_data("settings", db)


# ── Scheduler ──
