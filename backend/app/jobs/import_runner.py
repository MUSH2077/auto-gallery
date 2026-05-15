import asyncio
import json
import logging
import shutil
import time
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

POLL_INTERVAL = 5       # seconds between polls for new files
IDLE_TIMEOUT = 60        # seconds of no new files before declaring done


async def run_import_job(import_job_id: str):
    """Streaming import: polls for new JSON files as gallery-dl downloads them."""
    job_uuid = UUID(import_job_id)

    async with async_session() as db:
        result = await db.execute(select(ImportJob).where(ImportJob.id == job_uuid))
        import_job = result.scalar_one_or_none()
        if not import_job:
            return
        import_job.status = "running"
        await db.commit()

    dj_id = import_job.download_job_id
    flat_dir = Path(settings.download_root) / str(dj_id)
    provider = None
    creator_name = None
    source_creator_id = None
    processed_ids = set()
    last_activity = time.time()
    download_done = False

    while True:
        # Check download status
        if not download_done:
            async with async_session() as db:
                r = await db.execute(select(DownloadJob).where(DownloadJob.id == dj_id))
                dj = r.scalar_one_or_none()
                if dj and dj.status in ("downloaded", "failed", "complete"):
                    download_done = True

        # Gather current JSON files
        info_files = [p for p in flat_dir.rglob("*.json")
                      if not (p.name == "info.json" and p.parent == flat_dir)
                      and str(p) not in processed_ids]
        all_images = [p for p in flat_dir.rglob("*")
                      if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp")]

        if info_files:
            last_activity = time.time()

            # Lazy init provider + creator
            if provider is None:
                async with async_session() as db:
                    r2 = await db.execute(select(DownloadJob).where(DownloadJob.id == dj_id))
                    dj = r2.scalar_one_or_none()
                    if not dj:
                        return
                    provider = registry.get(dj.source)

                # Get creator name from first available JSON
                for info_file in info_files:
                    try:
                        with open(info_file) as f:
                            raw = json.load(f)
                        sc_data = provider.parse_source_creator(raw)
                        creator_name = (sc_data.get("display_name") or sc_data.get("source_creator_id", "unknown"))
                        creator_name = creator_name.replace("/", "_").replace("\\", "_").strip()
                        source_creator_id = sc_data.get("source_creator_id")
                        break
                    except Exception:
                        continue

                # Create SourceCreator
                async with async_session() as db:
                    existing_sc = await db.execute(
                        select(SourceCreator).where(
                            SourceCreator.source == provider.source_name,
                            SourceCreator.source_creator_id == source_creator_id,
                        )
                    )
                    if not existing_sc.scalar_one_or_none():
                        db.add(SourceCreator(
                            source=provider.source_name,
                            source_creator_id=source_creator_id,
                            display_name=creator_name,
                        ))
                        await db.commit()

            # Group by source_work_id
            groups = defaultdict(list)
            for info_file in info_files:
                try:
                    with open(info_file) as f:
                        raw = json.load(f)
                    ws = provider.parse_work_source(raw)
                    groups[ws["source_work_id"]].append((info_file, raw))
                except Exception:
                    processed_ids.add(str(info_file))

            # Process each work
            for src_work_id, group_items in groups.items():
                if not group_items:
                    continue
                first_file, first_raw = group_items[0]
                ws_data = provider.parse_work_source(first_raw)
                base = provider.source_name

                # Merge assets from all pages in the group
                all_ads = list(provider.parse_assets(first_raw, []))
                for _, extra_raw in group_items[1:]:
                    try:
                        all_ads.extend(provider.parse_assets(extra_raw, []))
                    except Exception:
                        pass

                # Match and move files
                work_flat_images = [p for p in all_images if src_work_id in p.stem]
                work_flat_images.sort(key=lambda p: p.stem)
                dl_work_dir = Path(settings.download_root) / base / creator_name / src_work_id
                dl_work_dir.mkdir(parents=True, exist_ok=True)
                lib_work_dir = Path(settings.library_root) / base / creator_name / src_work_id
                lib_work_dir.mkdir(parents=True, exist_ok=True)

                moved_files = []
                for idx in range(len(all_ads)):
                    source_file = None
                    for fp in work_flat_images:
                        if f"_p{idx}" in fp.stem and fp.exists():
                            source_file = fp
                            break
                    if not source_file and idx < len(work_flat_images):
                        source_file = work_flat_images[idx]
                    if not source_file or not source_file.exists():
                        continue

                    dest = dl_work_dir / source_file.name
                    if source_file.parent != dl_work_dir and not dest.exists():
                        shutil.move(str(source_file), str(dest))
                    moved_files.append((dest, all_ads[idx] if idx < len(all_ads) else None))

                if not moved_files:
                    # Mark all JSONs as processed even if no files matched
                    for info_file, _ in group_items:
                        processed_ids.add(str(info_file))
                    continue

                # Persist to DB
                async with async_session() as db:
                    # Work
                    work = Work(
                        title=ws_data.get("title"),
                        description=ws_data.get("description"),
                        posted_at=ws_data.get("posted_at"),
                    )
                    db.add(work)
                    await db.flush()

                    # WorkSource
                    work_source = WorkSource(
                        work_id=work.id, source=ws_data["source"],
                        source_work_id=src_work_id,
                        source_url=ws_data.get("source_url"),
                        source_creator_id=ws_data.get("source_creator_id"),
                        title=ws_data.get("title"), description=ws_data.get("description"),
                        posted_at=ws_data.get("posted_at"),
                        raw_metadata=ws_data.get("raw_metadata"),
                    )
                    db.add(work_source)
                    await db.flush()

                    for idx, (dest, ad) in enumerate(moved_files):
                        if not ad:
                            continue
                        dl_rel = str(dest.relative_to(settings.download_root))
                        asset = Asset(
                            file_name=dest.name, file_path=dl_rel,
                            file_size=dest.stat().st_size if dest.exists() else None,
                            width=ad.get("width"), height=ad.get("height"),
                            mime_type="image/jpeg" if dest.suffix.lower() in (".jpg", ".jpeg") else "image/png",
                        )
                        db.add(asset)
                        await db.flush()

                        if idx == 0:
                            from app.services.thumbnail import generate_thumbnail
                            thumb_path = generate_thumbnail(str(dest), lib_work_dir)
                            if thumb_path:
                                asset.thumb_sm_path = str(Path(thumb_path).relative_to(settings.library_root))
                            work.thumbnail_asset_id = str(asset.id)

                        db.add(AssetSource(
                            asset_id=asset.id, work_source_id=work_source.id,
                            source=ad.get("source", provider.source_name),
                            source_asset_id=ad.get("source_asset_id"),
                            source_url=ad.get("source_url"),
                            raw_metadata=ad.get("raw_metadata"),
                        ))
                        await db.flush()

                    # Tags
                    try:
                        tag_list = provider.parse_source_tags(first_raw)
                        seen = set()
                        for td in tag_list:
                            name = td.get("original_name", "").lower().strip()
                            if not name or name in seen:
                                continue
                            seen.add(name)
                            t = await db.execute(select(Tag).where(Tag.normalized_name == name))
                            tag = t.scalar_one_or_none()
                            if not tag:
                                tag = Tag(normalized_name=name, category=td.get("category"))
                                db.add(tag)
                                await db.flush()
                            if not (await db.execute(select(WorkTag).where(WorkTag.work_id == work.id, WorkTag.tag_id == tag.id))).scalar_one_or_none():
                                db.add(WorkTag(work_id=work.id, tag_id=tag.id, source=provider.source_name))
                            if not (await db.execute(select(WorkSourceTag).where(WorkSourceTag.work_source_id == work_source.id, WorkSourceTag.tag_id == tag.id))).scalar_one_or_none():
                                db.add(WorkSourceTag(work_source_id=work_source.id, tag_id=tag.id, source=td.get("source", provider.source_name), original_name=td.get("original_name")))
                    except NotImplementedError:
                        pass

                    # Metadata.json
                    try:
                        meta = {
                            "work_id": str(work.id), "source": provider.source_name,
                            "source_work_id": src_work_id, "title": ws_data.get("title"),
                            "posted_at": ws_data.get("posted_at"), "creator": creator_name,
                            "assets": all_ads,
                        }
                        with open(lib_work_dir / "metadata.json", "w") as mf:
                            json.dump(meta, mf, indent=2, ensure_ascii=False, default=str)
                    except Exception:
                        pass

                    await db.commit()

                # Mark JSONs as processed and delete them
                for info_file, _ in group_items:
                    try:
                        info_file.unlink()
                    except Exception:
                        pass
                    processed_ids.add(str(info_file))

        # Exit condition: download done AND no new files for IDLE_TIMEOUT
        if download_done and not info_files and (time.time() - last_activity > IDLE_TIMEOUT):
            break

        await asyncio.sleep(POLL_INTERVAL)

    # Final cleanup: remove empty flat dirs, orphaned files
    try:
        for img in flat_dir.rglob("*"):
            if img.is_file():
                try:
                    img.unlink()
                except Exception:
                    pass
        for d in sorted(flat_dir.rglob("*"), reverse=True):
            if d.is_dir() and d != flat_dir and not any(d.iterdir()):
                try:
                    d.rmdir()
                except Exception:
                    pass
    except Exception:
        pass

    async with async_session() as db:
        r = await db.execute(select(ImportJob).where(ImportJob.id == job_uuid))
        ij = r.scalar_one_or_none()
        if ij:
            ij.status = "complete"
            await db.commit()
