"""Cookie file paths shared by configuration and gallery-dl execution."""
import os
from pathlib import Path


class InvalidCookiePath(ValueError):
    pass


def resolve_cookie_path(value: str) -> Path:
    root = Path(os.environ.get("GALLERYDL_CONFIG_ROOT", "/gallerydl-config")).resolve()
    path = Path(value)
    # Previous configurations used the container mount and /cookies shorthand.
    if path.is_relative_to("/gallerydl-config"):
        path = root / path.relative_to("/gallerydl-config")
    elif path.is_relative_to("/cookies"):
        path = root / str(path).lstrip("/")
    elif not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError
    except (ValueError, OSError, RuntimeError) as exc:
        raise InvalidCookiePath("Cookie file must be inside the allowed configuration directory") from exc
    return resolved


def normalize_cookie_paths(config: dict) -> None:
    """Normalize file cookies, preserving inline cookie dictionaries."""
    for key, value in config.items():
        if key == "cookies" and isinstance(value, str) and value:
            config[key] = str(resolve_cookie_path(value))
        elif key != "cookies" and isinstance(value, dict):
            normalize_cookie_paths(value)
