import os
from pathlib import Path


def generate_thumbnail(source_path: str, library_dir: Path) -> str | None:
    """Generate thumbnail.webp for a work in its library directory. Returns relative path."""
    try:
        import pyvips
        image = pyvips.Image.new_from_file(source_path)
        w, h = image.width, image.height
        target_w = 400
        thumb_path = library_dir / "thumbnail.webp"
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        if w > target_w:
            thumb = image.thumbnail_image(target_w, height=target_w * 1000)
            thumb.webpsave(str(thumb_path), Q=80)
        else:
            image.webpsave(str(thumb_path), Q=80)
        return str(thumb_path)
    except Exception:
        return None
