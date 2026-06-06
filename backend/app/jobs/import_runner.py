import json
import logging
from collections import defaultdict
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import (
    Work, WorkSource, Asset, AssetSource, Tag, WorkTag, WorkSourceTag, SourceCreator,
)
from app.models.subscription import Subscription
from app.models.import_job import ImportJob
from app.models.download_job import DownloadJob
from app.providers import registry
from app.repositories.download_job import DownloadJobRepository
from app.services.job_manifest import append_manifest_event, update_manifest
from app.services.job_state import transition_import_job
from app.services.subscription_enqueue import mark_source_sync_success

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
ARCHIVE_EXTENSIONS = {".zip"}
ASSET_EXTENSIONS = IMAGE_EXTENSIONS | ARCHIVE_EXTENSIONS


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


def _get_image_dims(filepath: Path) -> tuple[int, int] | None:
    try:
        import pyvips
        img = pyvips.Image.new_from_file(str(filepath))
        return (img.width, img.height)
    except Exception:
        return None


def _mime_type(suffix: str) -> str:
    s = suffix.lower()
    if s in (".jpg", ".jpeg"):
        return "image/jpeg"
    if s == ".png":
        return "image/png"
    if s == ".webp":
        return "image/webp"
    if s == ".gif":
        return "image/gif"
    if s == ".zip":
        return "application/zip"
    return "application/octet-stream"


def _can_generate_thumbnail(suffix: str) -> bool:
    return suffix.lower() in IMAGE_EXTENSIONS


def _can_compute_phash(suffix: str) -> bool:
    return suffix.lower() in IMAGE_EXTENSIONS and suffix.lower() not in {".gif"}


async def run_import_job(import_job_id: str):
    job_uuid = UUID(import_job_id)
    async with async_session() as db:
        r = await db.execute(select(ImportJob).where(ImportJob.id == job_uuid))
        import_job = r.scalar_one_or_none()
        if not import_job:
            return
        transition_import_job(import_job, "running")
        await db.commit()

    try:
        async with async_session() as db:
            r2 = await db.execute(select(DownloadJob).where(
                DownloadJob.id == import_job.download_job_id))
            dj = r2.scalar_one_or_none()
        if not dj:
            return
        provider = registry.get(dj.source)

        # Get new JSON file paths from Redis (stored by download runner via snapshot diff).
        # This avoids re-scanning the filesystem which is unreliable due to:
        # - Naming template output directories not matching download_dir hint
        # - Concurrent imports processing the same JSONs
        # - Race conditions between snapshot and re-scan
        import redis as _redis
        _r = _redis.from_url(settings.redis_url)
        _files_key = f"import:{import_job_id}:files"
        _files_raw = _r.get(_files_key)
        _new_json_paths: list[str] | None = None
        if _files_raw:
            try:
                _new_json_paths = json.loads(_files_raw)
                _r.delete(_files_key)  # Clean up after reading
            except Exception:
                logger.warning("Failed to parse import file list for %s", import_job_id, exc_info=True)

        source_root = Path(settings.download_root) / provider.source_name

        if _new_json_paths:
            # Use the exact file list from the download runner's snapshot diff
            all_json_files = sorted(Path(p) for p in _new_json_paths if Path(p).exists())
            logger.info("Import %s: processing %d files from download snapshot (%d already gone)",
                       import_job_id, len(all_json_files), len(_new_json_paths) - len(all_json_files))
        else:
            # Fallback: scan filesystem (legacy path for jobs created before this fix)
            if not source_root.exists():
                async with async_session() as db:
                    ij = await db.get(ImportJob, job_uuid)
                    if ij:
                        transition_import_job(ij, "failed", f"Source directory not found: {source_root}")
                        await db.commit()
                return

            if dj.download_dir:
                candidate_root = source_root / dj.download_dir
                scan_root = candidate_root if candidate_root.exists() else source_root
            else:
                scan_root = source_root

            all_json_files = sorted(scan_root.rglob("*.json"))

        if not all_json_files:
            async with async_session() as db:
                ij = await db.get(ImportJob, job_uuid)
                if ij:
                    transition_import_job(ij, "failed", "No metadata JSON files found")
                    await db.commit()
            return

        # Group JSONs by work_id
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

        if not groups:
            async with async_session() as db:
                ij = await db.get(ImportJob, job_uuid)
                if ij:
                    transition_import_job(ij, "failed", "Could not extract work IDs from any JSON")
                    await db.commit()
            return

        stats = {"works": 0, "assets": 0, "multi_page": 0}
        batch_count = 0
        BATCH_SIZE = 50

        # Batch Meilisearch documents
        meili_docs = []

        for src_work_id, items in groups.items():
            if not items:
                continue

            first_file, first_raw = items[0]

            try:
                ws_data = provider.parse_work_source(first_raw)
                sc_data = provider.parse_source_creator(first_raw)
            except Exception:
                logger.warning("Failed to parse provider data for %s/%s", provider.source_name, src_work_id, exc_info=True)
                continue

            # Directory name: provider knows which field matches its gallery-dl template
            # Display name: for metadata.json
            dir_name = provider.get_creator_directory_name(first_raw)
            dir_name = dir_name.replace("/", "_").replace("\\", "_").strip()
            display_name = (sc_data.get("display_name") or dir_name)

            # Idempotency: skip if this work_source already exists
            async with async_session() as check_db:
                existing_ws = await check_db.execute(
                    select(WorkSource).where(
                        WorkSource.source == ws_data["source"],
                        WorkSource.source_work_id == src_work_id,
                    )
                )
                if existing_ws.scalar_one_or_none():
                    for jf, _ in items:
                        try:
                            jf.unlink()
                        except Exception:
                            logger.warning("Failed to unlink JSON %s", jf, exc_info=True)
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
                for jf, _ in items:
                    try:
                        jf.unlink()
                    except Exception:
                        logger.warning("Failed to unlink JSON %s", jf, exc_info=True)
                continue

            stats["works"] += 1
            if len(asset_files) > 1:
                stats["multi_page"] += 1
            stats["assets"] += len(asset_files)

            # Create DB records — files stay in place
            async with async_session() as db:
                # SourceCreator (upsert) — link to creator via subscription
                existing_sc = await db.execute(select(SourceCreator).where(
                    SourceCreator.source == sc_data["source"],
                    SourceCreator.source_creator_id == sc_data["source_creator_id"]))
                sc_obj = existing_sc.scalar_one_or_none()
                if not sc_obj:
                    # Find creator via download_job -> subscription
                    creator_id = None
                    if dj.subscription_id:
                        sub = await db.get(Subscription, dj.subscription_id)
                        if sub:
                            creator_id = sub.creator_id
                    db.add(SourceCreator(
                        source=sc_data["source"],
                        source_creator_id=sc_data["source_creator_id"],
                        source_url=sc_data.get("source_url"),
                        display_name=sc_data.get("display_name"),
                        creator_id=creator_id,
                    ))
                    await db.flush()
                elif sc_obj.creator_id is None and dj.subscription_id:
                    # Update existing source_creator with creator link
                    sub = await db.get(Subscription, dj.subscription_id)
                    if sub:
                        sc_obj.creator_id = sub.creator_id

                # Work
                is_ai = _detect_ai_generated(first_raw, provider.source_name)
                is_nsfw = _detect_nsfw(first_raw, provider.source_name)
                work = Work(title=ws_data.get("title"),
                            description=ws_data.get("description"),
                            posted_at=ws_data.get("posted_at"),
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
                                posted_at=ws_data.get("posted_at"),
                                raw_metadata=ws_data.get("raw_metadata"))
                db.add(ws)
                await db.flush()

                # Library dir uses display name (from JSON metadata)
                lib_dir = (Path(settings.library_root) / provider.source_name
                           / dir_name / src_work_id)
                lib_dir.mkdir(parents=True, exist_ok=True)

                # Assets from the media files (already in final location)
                for idx, fp in enumerate(asset_files):
                    dims = _get_image_dims(fp) if fp.suffix.lower() in IMAGE_EXTENSIONS else None
                    width, height = dims if dims else (None, None)

                    dl_rel = str(fp.relative_to(settings.download_root))
                    asset = Asset(
                        file_name=fp.name,
                        file_path=dl_rel,
                        file_size=fp.stat().st_size,
                        width=width,
                        height=height,
                        mime_type=_mime_type(fp.suffix),
                    )
                    db.add(asset)
                    await db.flush()

                    # Generate per-page thumbnail: {stem}.thumbnail.webp (e.g. 8232932_p0.thumbnail.webp)
                    if _can_generate_thumbnail(fp.suffix):
                        from app.services.thumbnail import generate_thumbnail
                        tp = generate_thumbnail(str(fp), lib_dir, name=f"{fp.stem}.thumbnail")
                        if tp:
                            asset.thumb_sm_path = str(
                                Path(tp).relative_to(settings.library_root))

                    # Compute pHash for image files (skip animated/video types)
                    if _can_compute_phash(fp.suffix):
                        try:
                            import imagehash
                            from PIL import Image as _PILImage
                            with _PILImage.open(str(fp)) as _pil_img:
                                asset.phash = str(imagehash.phash(_pil_img))
                        except Exception as _phash_err:
                            logger.warning("pHash failed for %s: %s", fp, _phash_err)

                    if idx == 0:
                        work.thumbnail_asset_id = str(asset.id)

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
                    assets_meta = []
                    for fp in asset_files:
                        assets_meta.append({"file_name": fp.name})
                    with open(lib_dir / "metadata.json", "w") as mf:
                        json.dump({
                            "work_id": str(work.id),
                            "source": provider.source_name,
                            "source_work_id": src_work_id,
                            "title": ws_data.get("title"),
                            "posted_at": ws_data.get("posted_at"),
                            "creator": display_name,
                            "assets": assets_meta,
                        }, mf, indent=2, ensure_ascii=False, default=str)
                except Exception:
                    logger.warning("Failed to write metadata.json for %s/%s", provider.source_name, src_work_id, exc_info=True)

                # Collect Meilisearch document for this work
                tag_names = [t.normalized_name for t in (await db.execute(
                    select(Tag.normalized_name).join(WorkTag).where(WorkTag.work_id == work.id)
                )).all()]
                asset_count = len(asset_files)
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
                    "posted_at": work.posted_at if work.posted_at else None,
                    "created_at": work.created_at.isoformat() if work.created_at else None,
                })

                # Commit work and batch Meilisearch periodically
                await db.commit()
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

            # Delete processed JSONs (keep image files)
            for jf, _ in items:
                try:
                    jf.unlink()
                except Exception:
                    logger.warning("Failed to unlink processed JSON %s", jf, exc_info=True)

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

        # Mark import complete and update parent download_job
        async with async_session() as db:
            ij = await db.get(ImportJob, job_uuid)
            if ij:
                transition_import_job(ij, "complete")
                # Also mark the parent download job as complete
                dj_repo = DownloadJobRepository(db)
                dj = await dj_repo.get(ij.download_job_id)
                if dj:
                    await dj_repo.update_status(dj, "complete")
                    update_manifest(dj, import_stats=stats)
                    append_manifest_event(dj, "import_complete", **stats)
                    if dj.subscription_source_id:
                        await mark_source_sync_success(db, dj.subscription_source_id)
                await db.commit()

        logger.info("Import complete: %d works, %d assets, %d multi-page (batched)",
                     stats["works"], stats["assets"], stats["multi_page"])

    except Exception as e:
        import traceback
        error_text = f"{str(e)[:1000]}\n{traceback.format_exc()[-500:]}"
        async with async_session() as db:
            ij = await db.get(ImportJob, job_uuid)
            if ij:
                # Check if this is a first failure — if so, retry once
                already_retried = ij.error_log and "RETRY_ATTEMPT" in (ij.error_log or "")
                if not already_retried:
                    transition_import_job(ij, "pending", f"RETRY_ATTEMPT\n{error_text}")
                    await db.commit()
                    # Re-enqueue with backoff
                    try:
                        import redis as redis_lib
                        from rq import Queue
                        from datetime import timedelta
                        r = redis_lib.from_url(settings.redis_url)
                        Queue(connection=r).enqueue_in(
                            timedelta(seconds=60),
                            "app.jobs.import_runner.run_import_job", import_job_id,
                            job_timeout=7200)
                        logger.info("Re-enqueued import job %s for retry", import_job_id)
                    except Exception:
                        logger.warning("Failed to enqueue import retry for %s", import_job_id, exc_info=True)
                else:
                    transition_import_job(ij, "failed", error_text)
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
