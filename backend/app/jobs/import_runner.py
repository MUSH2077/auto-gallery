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

        # Scan the permanent downloads directory (no job_id subdir)
        # gallery-dl config "directory": ["pixiv", "{user[account]}", "{id}"]
        # creates: /downloads/pixiv/{user[account]}/{work_id}/{work_id}_p0.jpg
        # with JSON: /downloads/pixiv/{user[account]}/{work_id}/{work_id}_p0.jpg.json
        source_root = Path(settings.download_root) / provider.source_name
        if not source_root.exists():
            async with async_session() as db:
                ij = await db.get(ImportJob, job_uuid)
                if ij:
                    ij.status = "failed"
                    ij.error_log = f"Source directory not found: {source_root}"
                    await db.commit()
            return

        # Find per-file metadata JSONs from --write-metadata
        all_json_files = sorted(source_root.rglob("*.json"))

        if not all_json_files:
            async with async_session() as db:
                ij = await db.get(ImportJob, job_uuid)
                if ij:
                    ij.status = "failed"
                    ij.error_log = "No metadata JSON files found"
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
                pass

        if not groups:
            async with async_session() as db:
                ij = await db.get(ImportJob, job_uuid)
                if ij:
                    ij.status = "failed"
                    ij.error_log = "Could not extract work IDs from any JSON"
                    await db.commit()
            return

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

            # Directory name: use account from raw JSON (matches gallery-dl {user[account]})
            # Display name: for metadata.json
            user_raw = first_raw.get("user", {})
            dir_name = (user_raw.get("account") or
                        sc_data.get("source_creator_id", "unknown"))
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
                            pass
                    continue

            # Image files are in the SAME directory as the JSONs
            # (gallery-dl per-work directories, no moving needed)
            work_dir = first_file.parent
            image_files = sorted(
                [p for p in work_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS],
                key=lambda p: p.stem,
            )

            if not image_files:
                for jf, _ in items:
                    try:
                        jf.unlink()
                    except Exception:
                        pass
                continue

            stats["works"] += 1
            if len(image_files) > 1:
                stats["multi_page"] += 1
            stats["assets"] += len(image_files)

            # Create DB records — files stay in place
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

                # Library dir uses display name (from JSON metadata)
                lib_dir = (Path(settings.library_root) / provider.source_name
                           / dir_name / src_work_id)
                lib_dir.mkdir(parents=True, exist_ok=True)

                # Assets from the image files (already in final location)
                for idx, fp in enumerate(image_files):
                    dims = _get_image_dims(fp)
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

                    if idx == 0:
                        from app.services.thumbnail import generate_thumbnail
                        tp = generate_thumbnail(str(fp), lib_dir)
                        if tp:
                            asset.thumb_sm_path = str(
                                Path(tp).relative_to(settings.library_root))
                        work.thumbnail_asset_id = str(asset.id)

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
                    for fp in image_files:
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
                    pass

                await db.commit()

            # Delete processed JSONs (keep image files)
            for jf, _ in items:
                try:
                    jf.unlink()
                except Exception:
                    pass

            # Remove empty directories (if no images left from other import runs)
            try:
                if not any(p for p in work_dir.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS):
                    continue
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
