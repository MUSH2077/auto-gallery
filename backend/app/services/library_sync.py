"""Shared library synchronization — metadata.json + thumbnails + FileIndex registration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path

from app.config import settings
from app.services.image_utils import can_generate_thumbnail
from app.services.media_assets import (
    browser_video_mime_type,
    is_browser_playable_video,
    media_kind,
    render_video_derivatives,
)

logger = logging.getLogger(__name__)
RENDERER_VERSION = "2"


def _lookup(metadata: dict, expression: str):
    value = metadata
    keys = [expression.split("[", 1)[0], *re.findall(r"\[([^]]+)\]", expression)]
    for actual in keys:
        if not isinstance(value, dict):
            return ""
        value = value.get(actual, "")
    return value


def _safe_segment(value: str) -> str:
    value = re.sub(r"[\\/\x00-\x1f]", "_", value.strip())
    value = re.sub(r"\{[^{}]+\}", "", value)
    return value[:240] or "unknown"


class LibraryPathResolver:
    def __init__(self, config: dict | None = None):
        self.config = config

    def creator_directory(self, source: str, metadata: dict | None, source_work_id: str) -> str:
        config = self.config
        if config is None:
            from app.services.settings import load_gallerydl_config
            config = load_gallerydl_config()
        from app.services.settings import extractor_key_for_source
        extractor = config.get("extractor", {}).get(extractor_key_for_source(source), {}) if config else {}
        template = extractor.get("directory", [source, "{id}"])
        raw = dict(metadata or {})
        raw.setdefault("id", source_work_id)

        def render(part) -> str:
            text = str(part)
            return _safe_segment(re.sub(
                r"\{([^{}]+)\}",
                lambda match: str(_lookup(raw, match.group(1)) or ""),
                text,
            ))

        parts = [render(part) for part in template]
        return parts[1] if len(parts) > 1 else _safe_segment(source_work_id)


def resolve_creator_directory(source: str, raw_metadata: dict | None, source_work_id: str,
                               config: dict | None = None) -> str:
    return LibraryPathResolver(config).creator_directory(source, raw_metadata, source_work_id)


def library_version(work, work_source, asset_rows: list) -> str:
    values = [
        RENDERER_VERSION, str(work.id), work.title or "",
        work.posted_at.isoformat() if work.posted_at else "",
        work_source.source, work_source.source_work_id,
    ]
    for asset, _asset_source in asset_rows:
        path = Path(settings.download_root) / asset.file_path
        try:
            stat = path.stat()
            values.extend((asset.file_path, str(stat.st_size), str(stat.st_mtime_ns)))
        except OSError:
            values.extend((asset.file_path, "missing", "0"))
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


async def sync_thumbnails(asset_rows: list, lib_dir: Path, file_index,
                           source: str, creator_dir: str, work_id: str,
                           force: bool = False) -> int:
    from app.services.thumbnail import generate_thumbnail
    generated = 0
    for asset, _as in asset_rows:
        fp = Path(settings.download_root) / asset.file_path
        if not fp.exists():
            continue
        kind = media_kind(asset.mime_type, asset.file_name)
        if kind == "video" and is_browser_playable_video(asset.mime_type, asset.file_name):
            try:
                expected_paths = {
                    lib_dir / f"{fp.stem}.thumbnail.webp",
                    lib_dir / f"{fp.stem}.poster.webp",
                }
                existing_paths = {path for path in expected_paths if path.exists()}
                derivatives = await asyncio.to_thread(
                    render_video_derivatives,
                    fp,
                    lib_dir,
                    fp.stem,
                    force=force,
                )
                if derivatives.inspection:
                    asset.width = derivatives.inspection.width
                    asset.height = derivatives.inspection.height
                    asset.duration = derivatives.inspection.duration
                    asset.mime_type = browser_video_mime_type(asset.file_name, asset.mime_type)
                produced = [
                    ("thumbnail", derivatives.thumbnail_path),
                    ("video_poster", derivatives.poster_path),
                ]
                for file_type, path in produced:
                    if not path:
                        continue
                    relative = str(path.relative_to(settings.library_root))
                    if file_type == "thumbnail":
                        asset.thumb_sm_path = relative
                    else:
                        asset.thumb_lg_path = relative
                    if file_index is not None:
                        file_index.upsert(
                            file_path=relative,
                            storage_root="library",
                            source=source,
                            creator_dir=creator_dir,
                            work_id=work_id,
                            file_name=path.name,
                            file_type=file_type,
                            file_size=path.stat().st_size,
                        )
                    if force or path not in existing_paths:
                        generated += 1
                if derivatives.error:
                    logger.warning(
                        "Video derivatives incomplete for asset %s: %s",
                        asset.id,
                        derivatives.error,
                    )
            except Exception:
                logger.warning("Video enrichment failed for %s", fp.name, exc_info=True)
        elif can_generate_thumbnail(fp.suffix):
            try:
                expected = lib_dir / f"{fp.stem}.thumbnail.webp"
                if not force and expected.exists() and expected.stat().st_mtime_ns >= fp.stat().st_mtime_ns:
                    continue
                tp = await asyncio.to_thread(generate_thumbnail, str(fp), lib_dir, f"{fp.stem}.thumbnail")
                if tp and file_index is not None:
                    rel = str(Path(tp).relative_to(settings.library_root))
                    file_index.upsert(file_path=rel, storage_root="library", source=source,
                                      creator_dir=creator_dir, work_id=work_id,
                                      file_name=Path(tp).name, file_type="thumbnail",
                                      file_size=Path(tp).stat().st_size)
                    generated += 1
            except Exception:
                logger.warning("Thumbnail failed for %s", fp.name, exc_info=True)
    return generated


def write_metadata_json(lib_dir: Path, work, work_source, creator_name: str,
                         assets_meta: list[dict], version: str | None = None) -> None:
    lib_dir.mkdir(parents=True, exist_ok=True)
    target = lib_dir / "metadata.json"
    temp = lib_dir / f".metadata.{uuid.uuid4().hex}.tmp"
    with open(temp, "w") as mf:
        json.dump({
            "work_id": str(work.id), "source": work_source.source,
            "source_work_id": work_source.source_work_id, "title": work.title,
            "posted_at": work.posted_at.isoformat() if work.posted_at else None,
            "creator": creator_name, "assets": assets_meta,
            "library_version": version,
        }, mf, indent=2, ensure_ascii=False, default=str)
        mf.flush()
        os.fsync(mf.fileno())
    os.replace(temp, target)


def metadata_version(lib_dir: Path) -> str | None:
    try:
        with open(lib_dir / "metadata.json") as handle:
            return json.load(handle).get("library_version")
    except (OSError, ValueError, TypeError):
        return None


def register_metadata_in_fileindex(file_index, source: str, creator_dir: str,
                                    work_id: str, lib_dir: Path) -> None:
    if file_index is None:
        return
    mf_path = lib_dir / "metadata.json"
    if not mf_path.exists():
        return
    file_index.upsert(
        file_path=str(Path(source) / creator_dir / work_id / "metadata.json"),
        storage_root="library", source=source, creator_dir=creator_dir,
        work_id=work_id, file_name="metadata.json",
        file_type="metadata_json", file_size=mf_path.stat().st_size,
        import_status="done")
