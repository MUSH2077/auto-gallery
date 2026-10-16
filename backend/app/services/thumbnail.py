import logging
import os
import sys
import uuid
from pathlib import Path

from app.services.image_utils import configure_pyvips, open_pillow_image

logger = logging.getLogger(__name__)


def inspect_and_generate_thumbnail(
    source_path: str,
    library_dir: Path,
    name: str = "thumbnail",
    *,
    working=None,
) -> tuple[str | None, int | None, int | None]:
    """Read original dimensions and produce a decoder-scaled card thumbnail.

    The header preserves original dimensions. The file-level thumbnail graph
    decodes once and can shrink JPEG pixels before allocating the full image.
    """
    image = None
    thumb = None
    temp_path = None
    try:
        target_w = 400
        thumb_path = library_dir / f"{name}.webp"
        temp_path = library_dir / f".{name}.{uuid.uuid4().hex}.tmp.webp"
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        if working is not None:
            # Preserve native cache trimming when libvips prepared the work,
            # without loading it for a small Pillow-only image.
            native = sys.modules.get("pyvips")
            if native is not None:
                configure_pyvips(native)
        if working is not None and working.original_size[0] > target_w:
            from PIL import Image
            display_width, display_height = working.display_size
            target_height = max(1, round(display_height * target_w / display_width))
            if target_w <= working.pixels.width and target_height <= working.pixels.height:
                with working.pixels.resize((target_w, target_height), Image.Resampling.LANCZOS) as resized:
                    resized.save(
                        temp_path, format="WEBP", quality=80,
                        icc_profile=resized.info.get("icc_profile", b""),
                    )
                os.replace(temp_path, thumb_path)
                return str(thumb_path), *working.original_size
        import pyvips

        configure_pyvips(pyvips)
        try:
            image = pyvips.Image.new_from_file(source_path, access="sequential")
        except Exception:
            if Path(source_path).suffix.lower() != ".avif":
                raise
            # Bookworm libvips/libheif cannot open AVIF sequences. Pillow can
            # decode their first frame without loading every frame into RAM.
            from PIL import Image

            with open_pillow_image(source_path) as opened:
                width, height = opened.size
                opened.thumbnail((target_w, height), Image.Resampling.LANCZOS)
                opened.save(temp_path, format="WEBP", quality=80)
            os.replace(temp_path, thumb_path)
            return str(thumb_path), width, height
        width, height = image.width, image.height
        if width > target_w:
            # The file-level operation can shrink JPEGs during decoding.
            # Keep the existing width/height policy and automatic orientation.
            thumb = pyvips.Image.thumbnail(source_path, target_w, height=target_w * 1000)
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
