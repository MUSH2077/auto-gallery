from app.services.image_utils import can_generate_thumbnail, get_mime_type, get_image_dims, can_compute_phash, IMAGE_EXTS
import asyncio
import json
import logging
import os
from time import monotonic
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session
from app.jobs.import_outcome import classify_import_outcome, clamp_threshold
from app.jobs.import_list_cleanup import cleanup_import_list, prune_import_lists
from app.jobs.worker_control import ControlListener, HeartbeatPublisher
from app.models import (
    Work, WorkSource, Asset, AssetSource, Tag, WorkTag, WorkSourceTag, SourceCreator,
)
from app.models.subscription import Subscription
from app.models.subscription_source import SubscriptionSource
from app.models.import_job import ImportJob
from app.models.download_job import DownloadJob
from app.providers import registry
from app.repositories.download_job import DownloadJobRepository
from app.services.job_progress import apply_download_progress, apply_import_progress
from app.services.job_manifest import append_manifest_event, update_manifest
from app.services.job_state import transition_import_job
from app.services.settings import get_download_defaults
from app.services.redis_client import get_redis
from app.services.subscription_enqueue import mark_source_sync_success
from app.services.curation import CurationService
from app.services.gitllery import project_commit_safe
from app.services.work_import import WorkImportService

logger = logging.getLogger(__name__)

ARCHIVE_EXTENSIONS = {".zip"}
ASSET_EXTENSIONS = IMAGE_EXTS | ARCHIVE_EXTENSIONS

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
        if raw.get("possibly_sensitive"):
            return True
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



async def run_import_job(import_job_id: str):
    job_uuid = UUID(import_job_id)
    async with async_session() as db:
        r = await db.execute(select(ImportJob).where(ImportJob.id == job_uuid))
        import_job = r.scalar_one_or_none()
        if not import_job:
            return
        transition_import_job(import_job, "running")
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

    # ── Start control listener + heartbeat ──
    listener = ControlListener(import_job_id)
    listener.start()
    heartbeat = HeartbeatPublisher(import_job_id, "import", pid=os.getpid())
    heartbeat.start()

    try:
        async with async_session() as db:
            r2 = await db.execute(select(DownloadJob).where(
                DownloadJob.id == import_job.download_job_id))
            dj = r2.scalar_one_or_none()
        if not dj:
            return
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
        from app.services.artifact_ledger import ArtifactLedger
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

        # Group JSONs by work_id ("parse" stage — timed for observability)
        _parse_started = monotonic()
        groups = defaultdict(list)
        for jf in all_json_files:
            try:
                with open(jf) as f:
                    raw = json.load(f)
                ws = provider.parse_work_source(raw)
                src_work_id = ws.get("source_work_id")
                if src_work_id:
                    groups[src_work_id].append((jf, raw))
            except Exception:
                logger.warning("Failed to parse JSON %s", jf, exc_info=True)
        _parse_ms = int((monotonic() - _parse_started) * 1000)

        if not groups:
            _empty_msg = "Could not extract work IDs from any JSON"
            async with async_session() as db:
                ij = await db.get(ImportJob, job_uuid)
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
            ij = await db.get(ImportJob, job_uuid)
            if ij:
                apply_import_progress(
                    ij,
                    "importing",
                    f"Importing {len(groups)} works",
                    current=0,
                    total=len(groups),
                )
                await db.commit()

        stats = {"works": 0, "assets": 0, "multi_page": 0, "skipped": 0, "existing": 0}
        batch_count = 0
        BATCH_SIZE = 50

        # Batch Meilisearch documents
        meili_docs = []

        _process_started = monotonic()  # "process" stage timing (per-work loop)
        for src_work_id, items in groups.items():
            # ── Safe interrupt point: check for pause/cancel signals ──
            if listener.should_stop():
                logger.info("Import %s: control signal received (%s), stopping at work %s",
                            import_job_id, listener.command, src_work_id)
                break

            if not items:
                continue

            first_file, first_raw = items[0]

            _json_rel = str(first_file.relative_to(settings.download_root))

            try:
                ws_data = provider.parse_work_source(first_raw)
                sc_data = provider.parse_source_creator(first_raw)
                posted_at = _parse_posted_at(ws_data.get("posted_at"))
            except Exception:
                logger.warning("Failed to parse provider data for %s/%s", provider.source_name, src_work_id, exc_info=True)
                stats["skipped"] += 1
                async with async_session() as ledger_db:
                    await ArtifactLedger(ledger_db).mark_work(dj.id, src_work_id, "failed", "provider metadata parse failed")
                    await ledger_db.commit()
                continue

            # Directory name: provider knows which field matches its gallery-dl template
            # Display name: for metadata.json
            dir_name = provider.get_creator_directory_name(first_raw)
            dir_name = dir_name.replace("/", "_").replace("\\", "_").strip()
            display_name = (sc_data.get("display_name") or dir_name)

            # ── SourceCreator resolution (runs BEFORE idempotency check
            # so retroactive linking fires even for already-imported works)
            sc_creator_id = None
            async with async_session() as sc_db:
                for sc_attempt in range(3):
                    try:
                        existing_sc = await sc_db.execute(select(SourceCreator).where(
                            SourceCreator.source == sc_data["source"],
                            SourceCreator.source_creator_id == sc_data["source_creator_id"]))
                        sc_obj = existing_sc.scalar_one_or_none()

                        if not sc_obj:
                            creator_id = None
                            ss = None
                            if dj.subscription_source_id:
                                ss = await sc_db.get(SubscriptionSource, dj.subscription_source_id)
                            should_link, _, _ = _should_auto_link_creator(provider, first_raw, ss)
                            if should_link and dj.subscription_id:
                                sub = await sc_db.get(Subscription, dj.subscription_id)
                                if sub:
                                    creator_id = sub.creator_id
                            sc_obj = SourceCreator(
                                source=sc_data["source"],
                                source_creator_id=sc_data["source_creator_id"],
                                source_url=sc_data.get("source_url"),
                                display_name=sc_data.get("display_name"),
                                creator_id=creator_id,
                            )
                            sc_db.add(sc_obj)
                            await sc_db.flush()
                            sc_creator_id = creator_id

                        elif sc_obj.creator_id is None:
                            ss = None
                            if dj.subscription_source_id:
                                ss = await sc_db.get(SubscriptionSource, dj.subscription_source_id)
                            should_link, _, _ = _should_auto_link_creator(provider, first_raw, ss)
                            if should_link and dj.subscription_id:
                                sub = await sc_db.get(Subscription, dj.subscription_id)
                                if sub and sub.creator_id:
                                    sc_obj.creator_id = sub.creator_id
                                    sc_creator_id = sub.creator_id
                                    logger.info(
                                        "Import %s: retroactively linked "
                                        "SourceCreator %s/%s to creator %s",
                                        import_job_id, sc_data["source"],
                                        sc_data["source_creator_id"], sub.creator_id,
                                    )
                        else:
                            sc_creator_id = sc_obj.creator_id

                        await sc_db.commit()
                        break
                    except IntegrityError:
                        if sc_attempt < 2:
                            await sc_db.rollback()
                            continue
                        raise

            # Idempotency: skip if this work_source already exists
            async with async_session() as check_db:
                existing_ws = await check_db.execute(
                    select(WorkSource).where(
                        WorkSource.source == ws_data["source"],
                        WorkSource.source_work_id == src_work_id,
                    )
                )
                if existing_ws.scalar_one_or_none():
                    stats["existing"] += 1
                    async with async_session() as ledger_db:
                        await ArtifactLedger(ledger_db).mark_work(dj.id, src_work_id, "done")
                        await ledger_db.commit()
                    continue

            # Media assets are in the SAME directory as the JSONs
            # (gallery-dl per-work directories, no moving needed)
            work_dir = first_file.parent
            asset_files = sorted(
                [p for p in work_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in ASSET_EXTENSIONS],
                key=lambda p: p.stem,
            )

            if not asset_files:
                stats["skipped"] += 1
                async with async_session() as ledger_db:
                    await ArtifactLedger(ledger_db).mark_work(dj.id, src_work_id, "failed", "no media assets found")
                    await ledger_db.commit()
                continue

            stats["works"] += 1
            if len(asset_files) > 1:
                stats["multi_page"] += 1
            stats["assets"] += len(asset_files)
            async with async_session() as progress_db:
                ij_progress = await progress_db.get(ImportJob, job_uuid)
                if ij_progress:
                    apply_import_progress(
                        ij_progress,
                        "importing",
                        f"Importing work {stats['works']} of {len(groups)}",
                        current=stats["works"],
                        total=len(groups),
                        assets=stats["assets"],
                    )
                    await progress_db.commit()

            # Create DB records — files stay in place
            async with async_session() as db:
                # Claim work in the same transaction so a crash rolls back the claim
                claimed = await ArtifactLedger(db).claim_work(
                    dj.id, src_work_id, job_uuid)
                if not claimed:
                    logger.info("Import %s: work %s already claimed or complete", import_job_id, src_work_id)
                    await db.rollback()
                    continue

                # SourceCreator already resolved above (before idempotency check).
                # sc_creator_id carries the linked creator_id for curation.
                linked_creator_id = sc_creator_id

                # Work
                is_ai = _detect_ai_generated(first_raw, provider.source_name)
                is_nsfw = _detect_nsfw(first_raw, provider.source_name)
                work = Work(title=ws_data.get("title"),
                            description=ws_data.get("description"),
                            posted_at=posted_at,
                            is_ai_generated=is_ai,
                            is_nsfw=is_nsfw)
                db.add(work)
                await db.flush()

                # WorkSource
                ws = WorkSource(work_id=work.id, source=ws_data["source"],
                                source_work_id=src_work_id,
                                source_url=ws_data.get("source_url"),
                                source_creator_id=ws_data.get("source_creator_id"),
                                title=ws_data.get("title"),
                                description=ws_data.get("description"),
                                posted_at=posted_at,
                                raw_metadata=ws_data.get("raw_metadata"))
                db.add(ws)
                await db.flush()

                _creator_dir, lib_dir = WorkImportService.library_directory(
                    provider.source_name, first_raw, src_work_id)
                lib_dir.mkdir(parents=True, exist_ok=True)

                # Assets from the media files (already in final location)
                for idx, fp in enumerate(asset_files):
                    dims = get_image_dims(fp) if fp.suffix.lower() in IMAGE_EXTS else None
                    width, height = dims if dims else (None, None)

                    dl_rel = str(fp.relative_to(settings.download_root))
                    asset = Asset(
                        file_name=fp.name,
                        file_path=dl_rel,
                        file_size=fp.stat().st_size,
                        width=width,
                        height=height,
                        mime_type=get_mime_type(fp.suffix),
                    )
                    db.add(asset)
                    await db.flush()

                    # Generate per-page thumbnail in thread pool (CPU-bound pyvips)
                    if can_generate_thumbnail(fp.suffix):
                        try:
                            from app.services.thumbnail import generate_thumbnail
                            tp = await asyncio.to_thread(
                                generate_thumbnail, str(fp), lib_dir, f"{fp.stem}.thumbnail")
                            if tp:
                                asset.thumb_sm_path = str(
                                    Path(tp).relative_to(settings.library_root))
                        except Exception as _thumb_err:
                            logger.warning("Thumbnail failed for %s: %s", fp, _thumb_err)

                    # Compute sha256 in thread pool (CPU-bound hashing)
                    try:
                        asset.sha256 = await asyncio.to_thread(WorkImportService.sha256, fp)
                    except Exception as _sha256_err:
                        logger.warning("sha256 failed for %s: %s", fp, _sha256_err)

                    # Compute pHash in thread pool (CPU-bound PIL)
                    if can_compute_phash(fp.suffix):
                        try:
                            asset.phash = await asyncio.to_thread(WorkImportService.phash, fp)
                        except Exception as _phash_err:
                            logger.warning("pHash failed for %s: %s", fp, _phash_err)

                    if idx == 0:
                        work.thumbnail_asset_id = asset.id

                    db.add(AssetSource(
                        asset_id=asset.id, work_source_id=ws.id,
                        source=provider.source_name,
                        source_asset_id=fp.stem,
                        source_url=None,
                        raw_metadata=None,
                    ))
                    await db.flush()

                # Tags
                try:
                    tags = provider.parse_source_tags(first_raw)
                    seen = set()
                    tag_names = []  # collected for Meilisearch, avoids re-query
                    for td in tags:
                        n = td.get("original_name", "").lower().strip()
                        if not n or n in seen:
                            continue
                        seen.add(n)
                        t = await db.execute(select(Tag).where(Tag.normalized_name == n))
                        tag = t.scalar_one_or_none()
                        if not tag:
                            tag = Tag(normalized_name=n, category=td.get("category"))
                            db.add(tag)
                            await db.flush()
                        if not (await db.execute(select(WorkTag).where(
                                WorkTag.work_id == work.id,
                                WorkTag.tag_id == tag.id))).scalar_one_or_none():
                            db.add(WorkTag(work_id=work.id, tag_id=tag.id,
                                           source=provider.source_name))
                            tag_names.append(n)
                        if not (await db.execute(select(WorkSourceTag).where(
                                WorkSourceTag.work_source_id == ws.id,
                                WorkSourceTag.tag_id == tag.id))).scalar_one_or_none():
                            db.add(WorkSourceTag(work_source_id=ws.id, tag_id=tag.id,
                                                 source=provider.source_name,
                                                 original_name=td.get("original_name")))
                except NotImplementedError:
                    pass

                # Write metadata.json to library
                try:
                    WorkImportService.write_library_metadata(
                        lib_dir, work, ws, display_name, asset_files)
                except Exception:
                    logger.warning("Failed to write metadata.json for %s/%s", provider.source_name, src_work_id, exc_info=True)

                curation = CurationService(db)
                curation_commit, curation_visibility = await curation.record_imported_work(
                    work,
                    creator_id=linked_creator_id,
                    repository_id=dj.subscription_source_id,
                    source=provider.source_name,
                    source_work_id=src_work_id,
                )

                # Collect Meilisearch document for this work
                # tag_names collected during tag processing above
                asset_count = len(asset_files)
                if curation_visibility == "visible":
                    meili_docs.append({
                        "id": str(work.id),
                        "title": work.title or "",
                        "description": (work.description or "")[:500],
                        "creator_name": display_name or "",
                        "is_nsfw": work.is_nsfw,
                        "is_ai_generated": work.is_ai_generated,
                        "source": provider.source_name,
                        "tags": [str(tn) for tn in tag_names],
                        "thumbnail_asset_id": str(work.thumbnail_asset_id) if work.thumbnail_asset_id else None,
                        "asset_count": asset_count,
                        "posted_at": _posted_at_json(work.posted_at),
                        "created_at": work.created_at.isoformat() if work.created_at else None,
                    })

                # Commit work and batch Meilisearch periodically
                await db.commit()
                if curation_commit is not None:
                    await project_commit_safe(db, curation_commit.id)
                batch_count += 1
                if batch_count >= BATCH_SIZE and meili_docs:
                    try:
                        from app.services.search import SearchService
                        svc = SearchService(db)
                        await svc._batch_index_works(meili_docs)
                    except Exception:
                        logger.warning("Batch Meilisearch index failed", exc_info=True)
                    meili_docs = []
                    batch_count = 0

            # Keep source metadata as an auditable, restart-safe input. The
            # ledger state prevents it from being imported again.
            async with async_session() as ledger_db:
                from app.services.artifact_ledger import managed_artifact_row
                ledger = ArtifactLedger(ledger_db)
                await ledger.mark_work(dj.id, src_work_id, "done")
                library_paths = [lib_dir / "metadata.json", *lib_dir.glob("*.thumbnail.webp")]
                await ledger.upsert_many([
                    managed_artifact_row(path, Path(settings.library_root), "library",
                                         provider.source_name, _creator_dir, src_work_id,
                                         "metadata_json" if path.name == "metadata.json" else "thumbnail")
                    for path in library_paths if path.exists()
                ])
                await ledger_db.commit()

            # Remove empty directories (no images left)
            try:
                remaining_assets = [p for p in work_dir.iterdir()
                                    if p.is_file() and p.suffix.lower() in ASSET_EXTENSIONS]
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
                logger.debug("Failed to check image files in %s", work_dir, exc_info=True)

        # Flush remaining Meilisearch batch
        if meili_docs:
            async with async_session() as db:
                try:
                    from app.services.search import SearchService
                    svc = SearchService(db)
                    await svc._batch_index_works(meili_docs)
                except Exception:
                    logger.warning("Failed to flush remaining Meilisearch batch", exc_info=True)

        # Stop control listener + heartbeat
        heartbeat.stop()
        listener.stop()

        # Determine final status based on control signal
        if listener.command == "pause":
            async with async_session() as db:
                ij = await db.get(ImportJob, job_uuid)
                if ij:
                    ij.status = "paused"
                    if listener.reason:
                        ij.user_note = listener.reason
                    apply_import_progress(
                        ij,
                        "paused",
                        f"Paused after {stats['works']} works",
                        current=stats["works"],
                        total=len(groups),
                        assets=stats["assets"],
                    )
                    from app.services.tasks import TaskService
                    await TaskService(db).update_subject(
                        "import_job",
                        ij.id,
                        status="paused",
                        progress=ij.progress_data,
                    )
                    await db.commit()
            logger.info("Import %s paused after %d works", import_job_id, stats["works"])
            return
        elif listener.command == "cancel":
            async with async_session() as db:
                ij = await db.get(ImportJob, job_uuid)
                if ij:
                    ij.status = "cancelled"
                    if listener.reason:
                        ij.user_note = listener.reason
                    apply_import_progress(
                        ij,
                        "cancelled",
                        f"Cancelled after {stats['works']} works",
                        current=stats["works"],
                        total=len(groups),
                        assets=stats["assets"],
                    )
                    from app.services.tasks import TaskService
                    await TaskService(db).update_subject(
                        "import_job",
                        ij.id,
                        status="cancelled",
                        progress=ij.progress_data,
                    )
                    await db.commit()
            logger.info("Import %s cancelled after %d works", import_job_id, stats["works"])
            return

        # ── Classify outcome (pure, unit-tested) ──
        # Note: total_groups is always >= 1 here — the empty case returned early
        # above ("if not groups"). classify_import_outcome's total_groups==0 rule is
        # a defensive safety net for the pure helper, not the live empty guard.
        total_groups = len(groups)
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
            ij = await db.get(ImportJob, job_uuid)
            if ij:
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
                    await dj_repo.update_status(dj, status, message if status == "failed" else None)
                    apply_download_progress(dj, status, message)
                    update_manifest(dj, import_stats=stats)
                    append_manifest_event(dj, "import_complete", status=status, **stats)
                    append_manifest_event(dj, "stage_timing", stage="parse", ms=_parse_ms)
                    append_manifest_event(dj, "stage_timing", stage="process", ms=_process_ms)
                    if status == "complete" and dj.subscription_source_id:
                        await mark_source_sync_success(db, dj.subscription_source_id)
                await db.commit()

        logger.info("Import finished: %d works, %d assets, %d skipped, %d multi-page (batched)",
                     stats["works"], stats["assets"], stats.get("skipped", 0), stats["multi_page"])

    except Exception as e:
        import traceback
        error_text = f"{str(e)[:1000]}\n{traceback.format_exc()[-500:]}"
        async with async_session() as db:
            ij = await db.get(ImportJob, job_uuid)
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
                    await db.commit()
                    # Re-enqueue with backoff (60s * retry_count)
                    try:
                        from rq import Queue
                        from datetime import timedelta
                        from app.services.redis_client import get_redis
                        backoff_seconds = 60 * retry_count
                        Queue(name="imports", connection=get_redis()).enqueue_in(
                            timedelta(seconds=backoff_seconds),
                            "app.jobs.import_runner.run_import_job", import_job_id,
                            job_timeout=7200)
                        logger.info("Re-enqueued import job %s for retry %d/%d",
                                   import_job_id, retry_count, max_retries)
                    except Exception:
                        logger.warning("Failed to enqueue import retry for %s", import_job_id, exc_info=True)
                else:
                    transition_import_job(ij, "failed",
                        f"Exhausted {max_retries} retries\n{error_text}")
                    apply_import_progress(
                        ij,
                        "failed",
                        "Import failed after all retries",
                    )
                    from app.services.tasks import TaskService
                    await TaskService(db).update_subject(
                        "import_job",
                        ij.id,
                        status="failed",
                        progress=ij.progress_data,
                        error=f"Exhausted {max_retries} retries\n{error_text}",
                    )
                    await db.commit()

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
