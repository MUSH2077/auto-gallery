import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def generate_thumbnail(source_path: str, library_dir: Path, name: str = "thumbnail") -> str | None:
    """Generate a WebP thumbnail in the library directory.

    Args:
        source_path: Full path to the source image file.
        library_dir: Target directory (created if needed).
        name: Output filename stem (without extension). Defaults to 'thumbnail'.
              Use the source file stem for per-page naming, e.g. '8232932_p0'.

    Returns the full path to the generated thumbnail, or None on failure.
    """
    image = None
    thumb = None
    try:
        import pyvips
        # access="sequential" streams the file instead of mmap-ing — much lower
        # RSS on 7.5 GB NAS for large images (e.g. 4K wallpapers, long strips).
        image = pyvips.Image.new_from_file(source_path, access="sequential")
        w, h = image.width, image.height
        target_w = 400
        thumb_path = library_dir / f"{name}.webp"
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        if w > target_w:
            thumb = image.thumbnail_image(target_w, height=target_w * 1000)
            thumb.webpsave(str(thumb_path), Q=80)
        else:
            image.webpsave(str(thumb_path), Q=80)
        return str(thumb_path)
    except Exception:
        logger.warning("Failed to generate thumbnail for %s", source_path, exc_info=True)
        return None
    finally:
        # Explicit deref so libvips GC runs immediately rather than waiting
        # for the Python GC cycle — critical under memory pressure.
        del thumb, image
