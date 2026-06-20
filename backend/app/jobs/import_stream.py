"""Streaming import worker — consumes works from Redis Stream "work:ready".

Replaces the batch import_job flow with real-time per-work processing.
Multiple workers can run in parallel via Redis Stream consumer groups.

Usage:
    python import_stream.py --group import-workers --consumer worker-1
    python worker_entrypoint.py --stream work:ready --group import-workers 2
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import traceback
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.jobs.worker_control import HeartbeatPublisher
from app.models import (
    Work, WorkSource, Asset, AssetSource, Tag, WorkTag, WorkSourceTag, SourceCreator,
)
from app.models.subscription import Subscription
from app.models.download_job import DownloadJob
from app.providers import registry
from app.services.curation import CurationService
from app.services.file_index import FileIndex
from app.services.redis_client import get_redis
from app.services.thumbnail import generate_thumbnail

# Reuse import_runner helpers
from app.jobs.import_runner import (
    _parse_posted_at, _posted_at_json, _detect_ai_generated, _detect_nsfw,
    _get_image_dims, _mime_type, _can_generate_thumbnail, _can_compute_phash,
)

logger = logging.getLogger(__name__)

STREAM_NAME = "work:ready"
GROUP_NAME = "import-workers"
BLOCK_MS = 5000
BATCH_SIZE = 10
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
ASSET_EXTENSIONS = IMAGE_EXTENSIONS | {".zip"}


async def process_work(msg: dict, consumer_id: str) -> bool:
    """Process a single work from the stream. Returns True on success (ACK)."""
    download_job_id = msg.get("download_job_id", "")
    source = msg.get("source", "")
    src_work_id = msg.get("work_id", "")
    json_path = msg.get("json_path", "")
    creator_dir = msg.get("creator_dir", "")

    if not src_work_id or not json_path:
        return True  # ACK malformed

    json_file = Path(settings.download_root) / json_path
    if not json_file.exists():
        return True

    # Idempotency: FileIndex status guard
    file_index = FileIndex(os.path.join(str(settings.download_root), ".file-index.sqlite3"))

    try:
        with open(json_file) as f:
            raw = json.load(f)
    except Exception:
        return True  # corrupt JSON — ACK and skip

    provider = registry.get(source)

    try:
        ws_data = provider.parse_work_source(raw)
    except Exception:
        return True  # unparseable — ACK and skip

    src_wid = ws_data.get("source_work_id") or src_work_id

    # Idempotency: skip if work_source already exists
    async with async_session() as check_db:
        existing = await check_db.execute(
            select(WorkSource).where(
                WorkSource.source == source,
                WorkSource.source_work_id == src_wid,
            )
        )
        if existing.scalar_one_or_none():
            file_index.mark_imported(json_path, "")
            return True

    # Media files from work directory
    work_dir = json_file.parent
    asset_files = sorted(
        [p for p in work_dir.iterdir()
         if p.is_file() and p.suffix.lower() in ASSET_EXTENSIONS],
        key=lambda p: p.stem,
    )
    if not asset_files:
        file_index.mark_imported(json_path, "")
        return True

    # ── DB transaction ──
    try:
        async with async_session() as db:
            dj = await db.get(DownloadJob, UUID(download_job_id)) if download_job_id else None

            # SourceCreator
            sc_data = provider.parse_source_creator(raw)
            linked_creator_id = None
            if sc_data:
                existing_sc = await db.execute(
                    select(SourceCreator).where(
                        SourceCreator.source == sc_data["source"],
                        SourceCreator.source_creator_id == sc_data["source_creator_id"],
                    )
                )
                sc_obj = existing_sc.scalar_one_or_none()
                if not sc_obj:
                    creator_id = None
                    if dj and dj.subscription_id:
                        sub = await db.get(Subscription, dj.subscription_id)
                        if sub:
                            creator_id = sub.creator_id
                    linked_creator_id = creator_id
                    db.add(SourceCreator(
                        source=sc_data["source"],
                        source_creator_id=sc_data["source_creator_id"],
                        source_url=sc_data.get("source_url"),
                        display_name=sc_data.get("display_name"),
                        creator_id=creator_id,
                    ))
                    await db.flush()
                elif sc_obj.creator_id is None and dj and dj.subscription_id:
                    sub = await db.get(Subscription, dj.subscription_id)
                    if sub:
                        sc_obj.creator_id = sub.creator_id
                        linked_creator_id = sub.creator_id

            posted_at = _parse_posted_at(ws_data.get("posted_at"))

            # Work
            work = Work(
                title=ws_data.get("title"),
                description=ws_data.get("description"),
                posted_at=posted_at,
                is_ai_generated=_detect_ai_generated(raw, source),
                is_nsfw=_detect_nsfw(raw, source),
            )
            db.add(work)
            await db.flush()

            # WorkSource
            ws = WorkSource(
                work_id=work.id, source=source,
                source_work_id=src_wid,
                source_url=ws_data.get("source_url"),
                source_creator_id=ws_data.get("source_creator_id"),
                title=ws_data.get("title"),
                description=ws_data.get("description"),
                posted_at=posted_at,
                raw_metadata=ws_data.get("raw_metadata"),
            )
            db.add(ws)
            await db.flush()

            # Library directory — use gallery-dl template
            display_name = sc_data.get("display_name") or creator_dir
            from app.services.settings import load_gallerydl_config, extractor_key_for_source
            _ek = extractor_key_for_source(source)
            _cfg = load_gallerydl_config()
            _ec = _cfg.get("extractor", {}).get(_ek, {})
            _dt = _ec.get("directory", [source, "{id}"])
            _rparts = []
            for part in _dt:
                rp = part
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(v, dict):
                            for sk, sv in v.items():
                                rp = rp.replace(f"{{user[{sk}]}}", str(sv) if sv else "")
                        elif isinstance(v, (str, int, float)):
                            rp = rp.replace(f"{{{k}}}", str(v) if v is not None else "")
                rp = rp.replace("{id}", src_wid)
                _rparts.append(rp.strip().replace("/", "_"))
            _crdir = _rparts[1] if len(_rparts) > 1 else creator_dir
            lib_dir = Path(settings.library_root) / source / _crdir / src_wid
            lib_dir.mkdir(parents=True, exist_ok=True)

            # Assets
            for idx, fp in enumerate(asset_files):
                dims = _get_image_dims(fp) if fp.suffix.lower() in IMAGE_EXTENSIONS else None
                width, height = dims if dims else (None, None)
                dl_rel = str(fp.relative_to(settings.download_root))
                asset = Asset(
                    file_name=fp.name, file_path=dl_rel,
                    file_size=fp.stat().st_size,
                    width=width, height=height,
                    mime_type=_mime_type(fp.suffix),
                )
                db.add(asset)
                await db.flush()

                if _can_generate_thumbnail(fp.suffix):
                    tp = await asyncio.to_thread(generate_thumbnail, str(fp), lib_dir, f"{fp.stem}.thumbnail")
                    if tp:
                        asset.thumb_sm_path = str(Path(tp).relative_to(settings.library_root))

                try:
                    def _sha256(filepath):
                        h = hashlib.sha256()
                        with open(filepath, "rb") as hf:
                            for chunk in iter(lambda: hf.read(8192), b""):
                                h.update(chunk)
                        return h.hexdigest()
                    asset.sha256 = await asyncio.to_thread(_sha256, str(fp))
                except Exception:
                    pass

                if _can_compute_phash(fp.suffix):
                    try:
                        import imagehash
                        from PIL import Image as PILImage
                        def _phash(filepath):
                            with PILImage.open(filepath) as img:
                                return str(imagehash.phash(img))
                        asset.phash = await asyncio.to_thread(_phash, str(fp))
                    except Exception:
                        pass

                if idx == 0:
                    work.thumbnail_asset_id = asset.id

                db.add(AssetSource(
                    asset_id=asset.id, work_source_id=ws.id,
                    source=source, source_asset_id=fp.stem,
                ))
                await db.flush()

            # Tags
            try:
                tags = provider.parse_source_tags(raw)
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
                    if not (await db.execute(select(WorkTag).where(WorkTag.work_id == work.id, WorkTag.tag_id == tag.id))).scalar_one_or_none():
                        db.add(WorkTag(work_id=work.id, tag_id=tag.id, source=source))
                    if not (await db.execute(select(WorkSourceTag).where(WorkSourceTag.work_source_id == ws.id, WorkSourceTag.tag_id == tag.id))).scalar_one_or_none():
                        db.add(WorkSourceTag(work_source_id=ws.id, tag_id=tag.id, source=source, original_name=td.get("original_name")))
            except NotImplementedError:
                pass

            # Library metadata.json
            try:
                assets_meta = [{"file_name": fp.name} for fp in asset_files]
                with open(lib_dir / "metadata.json", "w") as mf:
                    json.dump({
                        "work_id": str(work.id), "source": source,
                        "source_work_id": src_wid,
                        "title": ws_data.get("title"),
                        "posted_at": _posted_at_json(posted_at),
                        "creator": display_name, "assets": assets_meta,
                    }, mf, indent=2, ensure_ascii=False, default=str)
            except Exception:
                pass

            # Curation
            curation = CurationService(db)
            await curation.record_imported_work(
                work, creator_id=linked_creator_id,
                repository_id=dj.subscription_source_id if dj else None,
                source=source, source_work_id=src_wid,
            )
            await db.commit()

    except Exception:
        logger.error("Stream consumer %s: DB error for %s/%s: %s",
                    consumer_id, source, src_wid, traceback.format_exc()[-500:])
        return False

    # Cleanup
    file_index.mark_imported(json_path, "")
    try:
        json_file.unlink()
    except Exception:
        pass
    try:
        remaining = [p for p in work_dir.iterdir() if p.is_file() and p.suffix.lower() in ASSET_EXTENSIONS]
        if not remaining:
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
        pass

    logger.info("Stream consumer %s: imported %s/%s (%d assets)", consumer_id, source, src_wid, len(asset_files))
    return True


async def run_stream_consumer(consumer_id: str):
    """Main loop — consume from Redis Stream."""
    r = get_redis()
    logger.info("Stream consumer %s starting on %s/%s", consumer_id, STREAM_NAME, GROUP_NAME)

    try:
        r.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
    except Exception:
        pass

    heartbeat = HeartbeatPublisher(f"stream-{consumer_id}", "import", pid=os.getpid())
    heartbeat.start()

    try:
        while True:
            try:
                results = r.xreadgroup(GROUP_NAME, consumer_id, {STREAM_NAME: ">"}, count=BATCH_SIZE, block=BLOCK_MS)
            except Exception:
                await asyncio.sleep(1)
                continue

            if not results:
                continue

            for _stream_name, messages in results:
                for msg_id, msg_data in messages:
                    success = await process_work(msg_data, consumer_id)
                    if success:
                        r.xack(STREAM_NAME, GROUP_NAME, msg_id)

    except Exception:
        logger.critical("Stream consumer %s crashed: %s", consumer_id, traceback.format_exc()[-1000:])
    finally:
        heartbeat.stop()
