"""Shared image-type detection and metadata helpers used by import flows."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}

_MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".avif": "image/avif",
    ".mp4": "video/mp4", ".webm": "video/webm", ".zip": "application/zip",
}


def can_generate_thumbnail(suffix: str) -> bool:
    """Return True if pyvips can generate a thumbnail for this file type."""
    return suffix.lower() in IMAGE_EXTS


def get_mime_type(suffix: str) -> str | None:
    """Return standard MIME type for a file suffix, or None."""
    return _MIME_MAP.get(suffix.lower())


def get_image_dims(path: str) -> tuple[int, int] | None:
    """Return (width, height) via pyvips, or None on failure."""
    try:
        import pyvips
        img = pyvips.Image.new_from_file(path, access="sequential")
        return img.width, img.height
    except Exception:
        return None


def can_compute_phash(path: str) -> bool:
    """Return suffix eligibility; AVIF frame policy is checked during decoding."""
    return path.lower().endswith((".jpg", ".jpeg", ".png", ".avif"))


def open_pillow_image(path: str | Path) -> "Image.Image":
    """Open an image with one AVIF decoder thread per processing task."""
    from PIL import AvifImagePlugin, Image

    # Pillow otherwise uses CPU affinity, which ignores Docker CPU quotas and
    # can create many decoder threads even in a one-CPU worker container.
    AvifImagePlugin.DEFAULT_MAX_THREADS = 1
    return Image.open(path)
