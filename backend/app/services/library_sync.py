"""Shared library synchronization — metadata.json + thumbnails + FileIndex registration."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from app.config import settings
from app.services.image_utils import can_generate_thumbnail

logger = logging.getLogger(__name__)


def resolve_creator_directory(source: str, raw_metadata: dict | None, source_work_id: str,
                               config: dict | None = None) -> str:
    if config is None:
        from app.services.settings import load_gallerydl_config
        config = load_gallerydl_config()
    from app.services.settings import extractor_key_for_source
    ek = extractor_key_for_source(source)
    ec = config.get("extractor", {}).get(ek, {}) if config else {}
    dt = ec.get("directory", [source, "{id}"]) if ec else [source, "{id}"]
    rparts: list[str] = []
    rm = raw_metadata or {}
    for part in dt:
        rp = str(part)
        if isinstance(rm, dict):
            for k, v in rm.items():
                if isinstance(v, dict):
                    for sk, sv in v.items():
                        rp = rp.replace(f"{{user[{sk}]}}", str(sv) if sv else "")
                elif isinstance(v, (str, int, float)):
                    rp = rp.replace(f"{{{k}}}", str(v) if v is not None else "")
        rp = rp.replace("{id}", source_work_id)
        rparts.append(rp.strip().replace("/", "_"))
    return rparts[1] if len(rparts) > 1 else source_work_id


async def sync_thumbnails(asset_rows: list, lib_dir: Path, file_index,
                           source: str, creator_dir: str, work_id: str) -> int:
    from app.services.thumbnail import generate_thumbnail
    generated = 0
    for asset, _as in asset_rows:
        fp = Path(settings.download_root) / asset.file_path
        if fp.exists() and can_generate_thumbnail(fp.suffix):
            try:
                tp = await asyncio.to_thread(generate_thumbnail, str(fp), lib_dir, f"{fp.stem}.thumbnail")
                if tp:
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
                         assets_meta: list[dict]) -> None:
    lib_dir.mkdir(parents=True, exist_ok=True)
    with open(lib_dir / "metadata.json", "w") as mf:
        json.dump({
            "work_id": str(work.id), "source": work_source.source,
            "source_work_id": work_source.source_work_id, "title": work.title,
            "posted_at": work.posted_at.isoformat() if work.posted_at else None,
            "creator": creator_name, "assets": assets_meta,
        }, mf, indent=2, ensure_ascii=False, default=str)


def register_metadata_in_fileindex(file_index, source: str, creator_dir: str,
                                    work_id: str, lib_dir: Path) -> None:
    mf_path = lib_dir / "metadata.json"
    if not mf_path.exists():
        return
    file_index.upsert(
        file_path=str(Path(source) / creator_dir / work_id / "metadata.json"),
        storage_root="library", source=source, creator_dir=creator_dir,
        work_id=work_id, file_name="metadata.json",
        file_type="metadata_json", file_size=mf_path.stat().st_size,
        import_status="done")
