import json
import logging
import re
import shutil
from collections import defaultdict
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import (
    Work, WorkSource, Asset, AssetSource, Tag, WorkTag, WorkSourceTag, SourceCreator,
)
from app.models.import_job import ImportJob
from app.models.download_job import DownloadJob
from app.providers import registry

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


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
    return "application/octet-stream"


async def run_import_job(import_job_id: str):
    job_uuid = UUID(import_job_id)
    async with async_session() as db:
        r = await db.execute(select(ImportJob).where(ImportJob.id == job_uuid))
        import_job = r.scalar_one_or_none()
        if not import_job:
            return
        import_job.status = "running"
        await db.commit()

    try:
        async with async_session() as db:
            r2 = await db.execute(select(DownloadJob).where(
                DownloadJob.id == import_job.download_job_id))
            dj = r2.scalar_one_or_none()
        if not dj:
            return
        provider = registry.get(dj.source)

        job_dir = Path(settings.download_root) / str(dj.id)

        # Find per-file metadata JSONs from --write-metadata
        # These are {filename}.json alongside each image file
        # Exclude info.json at root (from --write-info-json, not used)
        all_json_files = sorted(
            [p for p in job_dir.rglob("*.json")
             if not (p.name == "info.json" and p.parent == job_dir)]
        )

        if not all_json_files:
            async with async_session() as db:
                ij = await db.get(ImportJob, job_uuid)
                if ij:
                    ij.status = "failed"
                    ij.error_log = "No metadata JSON files found"
                    await db.commit()
            return

        # Find all image files
        all_images = [p for p in job_dir.rglob("*")
                      if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS]

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
                pass

        if not groups:
            async with async_session() as db:
                ij = await db.get(ImportJob, job_uuid)
                if ij:
                    ij.status = "failed"
                    ij.error_log = "Could not extract work IDs from any JSON"
                    await db.commit()
            return

        creator_name = None
        stats = {"works": 0, "assets": 0, "multi_page": 0}

        for src_work_id, items in groups.items():
            if not items:
                continue

            first_file, first_raw = items[0]

            try:
                ws_data = provider.parse_work_source(first_raw)
                sc_data = provider.parse_source_creator(first_raw)
            except Exception:
                continue

            # Determine creator name from first work
            if creator_name is None:
                creator_name = (sc_data.get("display_name") or
                                sc_data.get("source_creator_id", "unknown"))
                creator_name = creator_name.replace("/", "_").replace("\\", "_").strip()

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
                            pass
                    continue

            # Match image files: filename stem contains the work_id
            # gallery-dl names files {id}_p{page}.{ext}
            work_images = [p for p in all_images if src_work_id in p.stem]
            work_images.sort(key=lambda p: p.stem)

            if not work_images:
                for jf, _ in items:
                    try:
                        jf.unlink()
                    except Exception:
                        pass
                continue

            # Target dir: downloads/{source}/{creator}/{work_id}/
            dl_work_dir = (Path(settings.download_root) / provider.source_name
                           / creator_name / src_work_id)
            dl_work_dir.mkdir(parents=True, exist_ok=True)

            # Move files
            moved = []
            for fp in work_images:
                dest = dl_work_dir / fp.name
                if fp.parent != dl_work_dir and not dest.exists():
                    shutil.move(str(fp), str(dest))
                moved.append(dest)

            if not moved:
                for jf, _ in items:
                    try:
                        jf.unlink()
                    except Exception:
                        pass
                continue

            stats["works"] += 1
            if len(moved) > 1:
                stats["multi_page"] += 1
            stats["assets"] += len(moved)

            # Create DB records
            async with async_session() as db:
                # SourceCreator (upsert)
                existing_sc = await db.execute(select(SourceCreator).where(
                    SourceCreator.source == sc_data["source"],
                    SourceCreator.source_creator_id == sc_data["source_creator_id"]))
                if not existing_sc.scalar_one_or_none():
                    db.add(SourceCreator(
                        source=sc_data["source"],
                        source_creator_id=sc_data["source_creator_id"],
                        source_url=sc_data.get("source_url"),
                        display_name=sc_data.get("display_name"),
                    ))
                    await db.flush()

                # Work
                work = Work(title=ws_data.get("title"),
                            description=ws_data.get("description"),
                            posted_at=ws_data.get("posted_at"))
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

                # Library dir
                lib_dir = (Path(settings.library_root) / provider.source_name
                           / creator_name / src_work_id)
                lib_dir.mkdir(parents=True, exist_ok=True)

                # Create assets from actual files
                for idx, dest in enumerate(moved):
                    dims = _get_image_dims(dest)
                    width, height = dims if dims else (None, None)

                    dl_rel = str(dest.relative_to(settings.download_root))
                    asset = Asset(
                        file_name=dest.name,
                        file_path=dl_rel,
                        file_size=dest.stat().st_size,
                        width=width,
                        height=height,
                        mime_type=_mime_type(dest.suffix),
                    )
                    db.add(asset)
                    await db.flush()

                    if idx == 0:
                        from app.services.thumbnail import generate_thumbnail
                        tp = generate_thumbnail(str(dest), lib_dir)
                        if tp:
                            asset.thumb_sm_path = str(
                                Path(tp).relative_to(settings.library_root))
                        work.thumbnail_asset_id = str(asset.id)

                    # AssetSource
                    db.add(AssetSource(
                        asset_id=asset.id, work_source_id=ws.id,
                        source=provider.source_name,
                        source_asset_id=f"{src_work_id}_p{idx}",
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
                    for dest in moved:
                        assets_meta.append({
                            "file_name": dest.name,
                            "width": width,
                            "height": height,
                        })
                    with open(lib_dir / "metadata.json", "w") as mf:
                        json.dump({
                            "work_id": str(work.id),
                            "source": provider.source_name,
                            "source_work_id": src_work_id,
                            "title": ws_data.get("title"),
                            "posted_at": ws_data.get("posted_at"),
                            "creator": creator_name,
                            "assets": assets_meta,
                        }, mf, indent=2, ensure_ascii=False, default=str)
                except Exception:
                    pass

                await db.commit()

            # Delete processed JSONs
            for jf, _ in items:
                try:
                    jf.unlink()
                except Exception:
                    pass

        # Clean up empty directories
        try:
            for d in sorted(job_dir.rglob("*"), reverse=True):
                if d.is_dir() and d != job_dir and not any(d.iterdir()):
                    d.rmdir()
        except Exception:
            pass

        async with async_session() as db:
            ij = await db.get(ImportJob, job_uuid)
            if ij:
                ij.status = "complete"
                await db.commit()

        logger.info("Import complete: %d works, %d assets, %d multi-page",
                     stats["works"], stats["assets"], stats["multi_page"])

    except Exception as e:
        import traceback
        async with async_session() as db:
            ij = await db.get(ImportJob, job_uuid)
            if ij:
                ij.status = "failed"
                ij.error_log = f"{str(e)[:1000]}\n{traceback.format_exc()[-500:]}"
                await db.commit()
