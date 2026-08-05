"""Media asset classification, video inspection, and derived poster rendering.

Callers provide a source path and library destination; this module hides the
ffprobe/ffmpeg invocation, representative-frame policy, and atomic writes.
Failures are returned as data so an otherwise valid import never fails merely
because optional playback metadata or posters could not be produced.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MediaKind = Literal["image", "animated_image", "video", "archive", "unknown"]

VIDEO_EXTENSIONS = {".mp4", ".webm"}
PLAYABLE_VIDEO_MIME_TYPES = {"video/mp4", "video/webm"}
ANIMATED_IMAGE_MIME_TYPES = {"image/gif", "image/apng"}
ARCHIVE_EXTENSIONS = {".zip"}


class MediaInspectionError(RuntimeError):
    """Raised when ffprobe cannot inspect a video."""


@dataclass(frozen=True, slots=True)
class VideoInspection:
    width: int | None
    height: int | None
    duration: float | None
    codec: str | None
    container: str | None


@dataclass(frozen=True, slots=True)
class VideoDerivatives:
    inspection: VideoInspection | None
    thumbnail_path: Path | None
    poster_path: Path | None
    error: str | None = None


def media_kind(mime_type: str | None, file_name: str | None = None) -> MediaKind:
    """Classify a stored asset using MIME first and extension as fallback."""

    mime = (mime_type or "").lower().strip()
    suffix = Path(file_name or "").suffix.lower()
    if mime.startswith("video/") or suffix in VIDEO_EXTENSIONS:
        return "video"
    if mime in ANIMATED_IMAGE_MIME_TYPES or suffix in {".gif", ".apng"}:
        return "animated_image"
    if mime.startswith("image/") or suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        return "image"
    if mime == "application/zip" or suffix in ARCHIVE_EXTENSIONS:
        return "archive"
    return "unknown"


def is_browser_playable_video(mime_type: str | None, file_name: str | None = None) -> bool:
    """Return whether the asset is in the first-party MP4/WebM playback scope."""

    mime = (mime_type or "").lower().split(";", 1)[0].strip()
    suffix = Path(file_name or "").suffix.lower()
    return mime in PLAYABLE_VIDEO_MIME_TYPES or suffix in VIDEO_EXTENSIONS


def browser_video_mime_type(file_name: str | None, current: str | None = None) -> str | None:
    """Normalize the stored MIME type for supported browser video containers."""

    suffix = Path(file_name or "").suffix.lower()
    if suffix == ".mp4":
        return "video/mp4"
    if suffix == ".webm":
        return "video/webm"
    mime = (current or "").lower().split(";", 1)[0].strip()
    return mime if mime in PLAYABLE_VIDEO_MIME_TYPES else current


def inspect_video(path: Path, timeout_seconds: int = 30) -> VideoInspection:
    """Read the first video stream and container duration with ffprobe."""

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name:stream=codec_name,codec_type,width,height,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaInspectionError(f"ffprobe unavailable or timed out: {type(exc).__name__}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown ffprobe error"
        raise MediaInspectionError(f"ffprobe failed: {detail[:300]}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaInspectionError("ffprobe returned invalid JSON") from exc
    streams = [
        stream
        for stream in payload.get("streams", [])
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    if not streams:
        raise MediaInspectionError("no video stream found")
    stream = streams[0]
    format_data = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration_raw = format_data.get("duration") or stream.get("duration")
    try:
        duration = max(0.0, float(duration_raw)) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration = None
    return VideoInspection(
        width=_positive_int(stream.get("width")),
        height=_positive_int(stream.get("height")),
        duration=duration,
        codec=str(stream.get("codec_name")) if stream.get("codec_name") else None,
        container=str(format_data.get("format_name")) if format_data.get("format_name") else None,
    )


def render_video_derivatives(
    source_path: Path,
    library_dir: Path,
    name: str,
    *,
    force: bool = False,
    probe_timeout_seconds: int = 30,
    render_timeout_seconds: int = 60,
) -> VideoDerivatives:
    """Inspect a video and atomically render small and large WebP posters."""

    try:
        inspection = inspect_video(source_path, timeout_seconds=probe_timeout_seconds)
    except MediaInspectionError as exc:
        return VideoDerivatives(None, None, None, str(exc))

    library_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_path = library_dir / f"{name}.thumbnail.webp"
    poster_path = library_dir / f"{name}.poster.webp"
    seek_seconds = min(3.0, max(0.1, (inspection.duration or 1.0) * 0.1))
    errors: list[str] = []

    for target, max_size in ((thumbnail_path, 400), (poster_path, 1280)):
        if not force and target.exists() and target.stat().st_mtime_ns >= source_path.stat().st_mtime_ns:
            continue
        error = _render_video_frame(
            source_path,
            target,
            seek_seconds=seek_seconds,
            max_size=max_size,
            timeout_seconds=render_timeout_seconds,
        )
        if error:
            errors.append(f"{target.name}: {error}")

    return VideoDerivatives(
        inspection=inspection,
        thumbnail_path=thumbnail_path if thumbnail_path.exists() else None,
        poster_path=poster_path if poster_path.exists() else None,
        error="; ".join(errors) or None,
    )


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _render_video_frame(
    source_path: Path,
    target_path: Path,
    *,
    seek_seconds: float,
    max_size: int,
    timeout_seconds: int,
) -> str | None:
    """Render one representative frame, retrying from time zero once."""

    for seek in (seek_seconds, 0.0):
        temp_path = target_path.with_name(f".{target_path.stem}.{uuid.uuid4().hex}.tmp.webp")
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{seek:.3f}",
                    "-i",
                    str(source_path),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    "1",
                    "-vf",
                    (
                        f"scale=w='min({max_size},iw)':h='min({max_size},ih)'"
                        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
                    ),
                    "-c:v",
                    "libwebp",
                    "-quality",
                    "80",
                    "-y",
                    str(temp_path),
                ],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            if result.returncode == 0 and temp_path.exists() and temp_path.stat().st_size > 0:
                os.replace(temp_path, target_path)
                return None
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no frame produced"
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = f"ffmpeg unavailable or timed out: {type(exc).__name__}"
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
    return detail[:300]
