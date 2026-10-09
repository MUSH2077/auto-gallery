"""Reusable per-work media operations shared by import entry points."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import settings
from app.services.image_utils import configure_pyvips, open_pillow_image
from app.services.library_sync import resolve_creator_directory, write_metadata_json


# Below this size Pillow's straightforward path has a small working set.
PNG_PHASH_STREAM_MIN_PIXELS = 16_000_000


def _png_phash_sample(path: Path, image):
    """Return a direct 32x32 grayscale sample without a full RGB bitmap."""
    if image.mode not in ("RGB", "RGBA") or getattr(image, "n_frames", 1) != 1:
        return None
    with path.open("rb") as handle:
        header = handle.read(29)
    # Limit this path to non-interlaced 8-bit PNG. Other layouts retain the
    # original Pillow decoder, including its existing malformed-file behavior.
    if len(header) != 29 or header[24] != 8 or header[28] != 0:
        return None

    import pyvips
    from PIL import Image

    configure_pyvips(pyvips)
    rgb = pyvips.Image.new_from_file(str(path), access="sequential", fail_on="error")
    # Pillow's RGB/RGBA -> L uses these fixed-point coefficients and ignores
    # alpha. Every intermediate integer is < 2**24, exactly representable in
    # libvips float arithmetic. Do not substitute colourspace()/floating luma.
    gray = ((rgb[0] * 19595 + rgb[1] * 38470 + rgb[2] * 7471 + 32768) / 65536).floor().cast("uchar")
    buffer = gray.write_to_memory()
    # L is a Pillow mapped mode: this borrows the buffer instead of making a
    # second full-size grayscale copy. Resize while the buffer is still alive.
    with Image.frombuffer("L", image.size, buffer, "raw", "L", 0, 1) as mapped:
        return mapped.resize((32, 32), Image.Resampling.LANCZOS)


class WorkImportService:
    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def phash(path: Path, *, working=None, oriented=False) -> str | None:
        from app.services.perceptual_hash import grayscale_sample, phash_from_sample

        if working is not None:
            return working.phash()
        sample = None
        with open_pillow_image(path) as image:
            # AVIF sequences retain exact-file deduplication, not a first-frame hash.
            if image.format == "AVIF" and image.n_frames > 1:
                return None
            if image.format == "PNG" and image.width * image.height >= PNG_PHASH_STREAM_MIN_PIXELS:
                sample = _png_phash_sample(path, image)
                if sample is not None and oriented:
                    from PIL import ImageOps
                    from app.services.visual_image import image_orientation
                    sample.getexif()[274] = image_orientation(image, path) or 1
                    ImageOps.exif_transpose(sample, in_place=True)
            if sample is None:
                if oriented:
                    from PIL import ImageOps
                    ImageOps.exif_transpose(image, in_place=True)
                sample = grayscale_sample(image)
        # The full decoded bitmap is released before loading the numeric library.
        with sample:
            pixels = sample.tobytes()
        return phash_from_sample(pixels)

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
