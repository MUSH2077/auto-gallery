import logging
import os
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# libvips reads its concurrency environment during initialization.  Set this
# before the lazy ``import pyvips`` below so a single large image cannot fan out
# across all NAS cores and multiply its working set.
os.environ.setdefault("VIPS_CONCURRENCY", "1")
try:
    VIPS_CACHE_MAX_MEMORY = min(
        64,
        max(
            1,
            int(os.environ.get("VIPS_CACHE_MAX_MEM_MB", "64")),
        ),
    ) * 1024 * 1024
except ValueError:
    VIPS_CACHE_MAX_MEMORY = 64 * 1024 * 1024


def _configure_pyvips(pyvips) -> None:
    """Apply the process-wide libvips cache ceiling idempotently."""

    pyvips.cache_set_max_mem(VIPS_CACHE_MAX_MEMORY)


def inspect_and_generate_thumbnail(
    source_path: str,
    library_dir: Path,
    name: str = "thumbnail",
) -> tuple[str | None, int | None, int | None]:
    """Read an image once with sequential libvips access.

    Width/height metadata and the card thumbnail are produced from the same
    lazy libvips graph.  This replaces the importer's old ``get_image_dims`` +
    ``generate_thumbnail`` pair, which opened every source image twice.
    """
    image = None
    thumb = None
    temp_path = None
    try:
        import pyvips

        _configure_pyvips(pyvips)
        image = pyvips.Image.new_from_file(source_path, access="sequential")
        width, height = image.width, image.height
        target_w = 400
        thumb_path = library_dir / f"{name}.webp"
        temp_path = library_dir / f".{name}.{uuid.uuid4().hex}.tmp.webp"
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        if width > target_w:
            thumb = image.thumbnail_image(target_w, height=target_w * 1000)
            thumb.webpsave(str(temp_path), Q=80)
        else:
            image.webpsave(str(temp_path), Q=80)
        os.replace(temp_path, thumb_path)
        return str(thumb_path), width, height
    except Exception:
        try:
            if temp_path and temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        logger.warning("Failed to inspect/generate thumbnail for %s", source_path, exc_info=True)
        return None, None, None
    finally:
        del thumb, image


def generate_thumbnail(source_path: str, library_dir: Path, name: str = "thumbnail") -> str | None:
    """Generate a WebP thumbnail in the library directory.

    Args:
        source_path: Full path to the source image file.
        library_dir: Target directory (created if needed).
        name: Output filename stem (without extension). Defaults to 'thumbnail'.
              Use the source file stem for per-page naming, e.g. '8232932_p0'.

    Returns the full path to the generated thumbnail, or None on failure.
    """
    path, _width, _height = inspect_and_generate_thumbnail(
        source_path,
        library_dir,
        name,
    )
    return path
