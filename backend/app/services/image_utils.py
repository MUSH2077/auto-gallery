"""Shared image-type detection and metadata helpers used by import flows."""

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

_MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
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
    """Return True if perceptual hash can be computed for this file."""
    return path.lower().endswith((".jpg", ".jpeg", ".png"))
