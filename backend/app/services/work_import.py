"""Reusable per-work media operations shared by import entry points."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import settings
from app.services.library_sync import resolve_creator_directory, write_metadata_json


class WorkImportService:
    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def phash(path: Path) -> str:
        import imagehash
        from PIL import Image
        with Image.open(path) as image:
            return str(imagehash.phash(image))

    @staticmethod
    def library_directory(source: str, raw_metadata: dict, source_work_id: str) -> tuple[str, Path]:
        creator_dir = resolve_creator_directory(source, raw_metadata, source_work_id)
        return creator_dir, Path(settings.library_root) / source / creator_dir / source_work_id

    @staticmethod
    def write_library_metadata(
        lib_dir: Path,
        work,
        work_source,
        creator_name: str,
        asset_files: list[Path],
    ) -> bool:
        from app.services.library_sync import RENDERER_VERSION
        values = [
            RENDERER_VERSION, str(work.id), work.title or "",
            work.posted_at.isoformat() if work.posted_at else "",
            work_source.source, work_source.source_work_id,
        ]
        for fp in asset_files:
            try:
                stat = fp.stat()
                values.extend((fp.name, str(stat.st_size), str(stat.st_mtime_ns)))
            except OSError:
                values.extend((fp.name, "missing", "0"))
        version = hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()
        return write_metadata_json(
            lib_dir, work, work_source, creator_name,
            [{"file_name": path.name} for path in asset_files],
            version=version,
        )
