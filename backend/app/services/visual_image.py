"""Bounded, oriented pixels shared by visual fingerprints and SSIM."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image
from collections import OrderedDict
import os
import struct
from uuid import UUID, uuid4
import logging

logger = logging.getLogger(__name__)

from app.config import settings

from app.services.image_utils import configure_pyvips, open_pillow_image

WORK_EDGE = 1024
SHARED_MIN_PIXELS = 16_000_000
SMALL_WORK_MAX_PIXELS = 8_000_000
PHASH_VERSION = "numpy-phash-oriented-adaptive-v3"


@dataclass
class WorkingImage:
    pixels: "Image.Image"
    original_size: tuple[int, int]
    display_size: tuple[int, int]
    cache_sample: bool = True

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.pixels.close()

    def phash(self) -> str:
        from app.services.perceptual_hash import phash
        return phash(self.pixels)

    def sample(self) -> bytes:
        from PIL import Image
        rgb = self.pixels if self.pixels.mode == "RGB" else self.pixels.convert("RGB")
        try:
            with rgb.resize((256, 256), Image.Resampling.LANCZOS) as sample:
                return sample.tobytes()
        finally:
            if rgb is not self.pixels:
                rgb.close()


def image_orientation(image, path: Path):
    # PNG getexif() loads the full bitmap when metadata wasn't found before
    # IDAT. Scan chunk headers with seek() instead, including trailing eXIf.
    if image.format == "PNG" and not image.info.get("exif"):
        from PIL import Image
        with path.open("rb") as handle:
            handle.seek(8)
            while chunk := handle.read(8):
                if len(chunk) != 8:
                    break
                size, kind = struct.unpack(">I4s", chunk)
                if kind == b"eXIf":
                    exif = Image.Exif()
                    exif.load(handle.read(size))
                    return exif.get(274)
                if kind == b"IEND":
                    break
                handle.seek(size + 4, 1)
        return None
    return image.getexif().get(274)


def display_dimensions(path: Path) -> tuple[int, int]:
    with open_pillow_image(path) as image:
        width, height = image.size
        if image_orientation(image, path) in (5, 6, 7, 8):
            width, height = height, width
        return width, height




def _share_eligible(path: Path, image) -> bool:
    if image.width * image.height < SHARED_MIN_PIXELS:
        return False
    if min(image.size) * 4 < max(image.size):
        return False
    if image.format == "AVIF" and image.n_frames > 1:
        return False
    if image.format == "PNG":
        if getattr(image, "n_frames", 1) != 1:
            return False
        with path.open("rb") as handle:
            header = handle.read(29)
        return (image.mode in ("RGB", "RGBA") and len(header) == 29
                and header[24] == 8 and header[28] == 0)
    # Keep CMYK interpretation and uncommon PNG layouts on Pillow's existing
    # path; the large-image measurements cover native RGB decode/shrink.
    return image.mode != "CMYK"

def prepare_visual_work(path: Path) -> WorkingImage | None:
    with open_pillow_image(path) as image:
        if not _share_eligible(path, image):
            return None
    return working_image(path)


def prepare_small_visual_work(path: Path) -> WorkingImage | None:
    # Decode a bounded card once for both thumbnail and direct pHash sampling.
    # Keeping these JPEG/PNG tasks on Pillow also avoids loading a second
    # native image stack just to create their card thumbnail.
    from PIL import ImageOps

    image = open_pillow_image(path)
    try:
        if (image.format not in ("JPEG", "PNG") or image.mode not in ("RGB", "RGBA")
                or image.width <= 400 or image.width * image.height > SMALL_WORK_MAX_PIXELS
                or (image.mode == "RGB" and "transparency" in image.info)
                or getattr(image, "n_frames", 1) != 1):
            image.close()
            return None
        original_size = image.size
        ImageOps.exif_transpose(image, in_place=True)
        return WorkingImage(image, original_size, image.size, cache_sample=False)
    except Exception:
        image.close()
        raise


def analysis_pixels(path: Path) -> tuple[bytes, tuple[int, int]]:
    from PIL import Image, ImageOps
    with open_pillow_image(path) as image:
        if not _share_eligible(path, image):
            ImageOps.exif_transpose(image, in_place=True)
            display_size = image.size
            rgb = image if image.mode == "RGB" else image.convert("RGB")
            try:
                with rgb.resize((256, 256), Image.Resampling.LANCZOS) as sample:
                    return sample.tobytes(), display_size
            finally:
                if rgb is not image:
                    rgb.close()
    with working_image(path) as work:
        return work.sample(), work.display_size

def working_image(path: Path, *, edge: int = WORK_EDGE) -> WorkingImage | None:
    from PIL import Image, ImageOps
    # Keep Pillow's decompression-bomb validation before native decoding.
    with open_pillow_image(path) as header:
        original_size = header.size
        if header.format == "AVIF" and header.n_frames > 1:
            return None
        orientation = image_orientation(header, path)
    display_size = original_size[::-1] if orientation in (5, 6, 7, 8) else original_size

    import pyvips
    configure_pyvips(pyvips)
    try:
        scaled = pyvips.Image.thumbnail(str(path), edge, height=edge, size="down", fail_on="error", no_rotate=True)
        if scaled.interpretation != "srgb":
            scaled = scaled.colourspace("srgb")
        if scaled.format != "uchar":
            scaled = scaled.cast("uchar")
        mode = "RGBA" if scaled.hasalpha() else "RGB"
        pixels = Image.frombytes(mode, (scaled.width, scaled.height), scaled.write_to_memory())
        transforms = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
                      4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE,
                      6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
                      8: Image.Transpose.ROTATE_90}
        if orientation in transforms:
            oriented = pixels.transpose(transforms[orientation])
            pixels.close()
            pixels = oriented
    except pyvips.Error:
        if path.suffix.lower() != ".avif":
            raise
        # Some libheif builds cannot decode AVIF; retain the existing Pillow path.
        with open_pillow_image(path) as image:
            image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            ImageOps.exif_transpose(image, in_place=True)
            pixels = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    return WorkingImage(pixels, original_size, display_size)


# Fixed-size slots bound disk usage to ~193 MiB, without directory scans or an
# eviction database in the hot path. Slot collisions are ordinary cache misses.
CACHE_SLOTS = 1024
CACHE_HEADER = struct.Struct(">8s16sQQII32s")
CACHE_MAGIC = b"AGVA0016"
SAMPLE_BYTES = 256 * 256 * 3
MEMORY_CACHE_BYTES = 16 * 1024 * 1024


def sample_cache_relative(asset_id: UUID) -> str:
    return f".visual-samples/{asset_id.int % CACHE_SLOTS:04x}.rgb"


def _cache_path(asset_id: UUID) -> Path:
    return Path(settings.library_root) / sample_cache_relative(asset_id)


def read_sample(path: Path, asset_id: UUID, sha256: str):
    stat = path.stat()
    try:
        with _cache_path(asset_id).open("rb") as handle:
            magic, owner, size, mtime, width, height, sha = CACHE_HEADER.unpack(handle.read(CACHE_HEADER.size))
            if (magic, owner, size, mtime, sha) != (
                CACHE_MAGIC, asset_id.bytes, stat.st_size, stat.st_mtime_ns, bytes.fromhex(sha256)
            ):
                return None
            pixels = handle.read(SAMPLE_BYTES + 1)
            if len(pixels) != SAMPLE_BYTES:
                return None
            return pixels, (width, height)
    except (OSError, ValueError, struct.error):
        return None


def save_sample(path: Path, asset_id: UUID, sha256: str, work: WorkingImage) -> None:
    _write_sample(path, asset_id, sha256, work.sample(), work.display_size)


def _write_sample(path: Path, asset_id: UUID, sha256: str,
                  pixels: bytes, display_size: tuple[int, int]) -> None:
    stat = path.stat()
    target = _cache_path(asset_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        header = CACHE_HEADER.pack(CACHE_MAGIC, asset_id.bytes, stat.st_size, stat.st_mtime_ns,
                                   *display_size, bytes.fromhex(sha256))
        with temporary.open("xb") as handle:
            handle.write(header)
            handle.write(pixels)
        current = path.stat()
        if (current.st_size, current.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise RuntimeError("Image changed while generating its visual sample")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def owned_sample_relative(asset_id: UUID) -> str | None:
    """Only include this asset's slot in normal asset-file cleanup."""
    try:
        with _cache_path(asset_id).open("rb") as handle:
            magic, owner, *_ = CACHE_HEADER.unpack(handle.read(CACHE_HEADER.size))
        if magic == CACHE_MAGIC and owner == asset_id.bytes:
            return sample_cache_relative(asset_id)
    except (OSError, struct.error):
        pass
    return None


class SampleCache:
    """One reconciliation's bounded byte cache; float arrays are not retained."""
    def __init__(self, *, limit_bytes: int = MEMORY_CACHE_BYTES):
        self.limit_bytes = limit_bytes
        self.entries = OrderedDict()
        self.bytes = 0

    def get(self, path: Path, *, asset_id: UUID | None = None, sha256: str | None = None):
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns, asset_id, sha256)
        if key in self.entries:
            self.entries.move_to_end(key)
            return self.entries[key]
        sample = read_sample(path, asset_id, sha256) if asset_id and sha256 else None
        if sample is None:
            sample = analysis_pixels(path)
            if asset_id and sha256:
                try:
                    _write_sample(path, asset_id, sha256, *sample)
                except OSError:
                    logger.warning("Unable to cache analysis sample for %s", path, exc_info=True)
        if self.limit_bytes >= SAMPLE_BYTES:
            while self.entries and self.bytes + SAMPLE_BYTES > self.limit_bytes:
                self.entries.popitem(last=False)
                self.bytes -= SAMPLE_BYTES
            self.entries[key] = sample
            self.bytes += SAMPLE_BYTES
        return sample

    def clear(self):
        self.entries.clear()
        self.bytes = 0
