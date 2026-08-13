import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.media_assets import (
    VideoInspection,
    browser_video_mime_type,
    _render_video_frame,
    inspect_video,
    is_browser_playable_video,
    media_kind,
    render_video_derivatives,
)


def _make_video(path: Path, suffix: str = ".mp4") -> Path:
    target = path.with_suffix(suffix)
    codec = "libvpx-vp9" if suffix == ".webm" else "libx264"
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:d=1",
            "-c:v",
            codec,
            "-pix_fmt",
            "yuv420p",
            "-y",
            str(target),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return target


@pytest.mark.parametrize(
    ("mime", "name", "expected"),
    [
        ("video/mp4", "clip.bin", "video"),
        (None, "clip.webm", "video"),
        ("image/gif", "still.bin", "animated_image"),
        ("image/png", "still.png", "image"),
        ("application/zip", "frames.zip", "archive"),
        (None, "asset.bin", "unknown"),
    ],
)
def test_media_kind_classification(mime, name, expected):
    assert media_kind(mime, name) == expected


@pytest.mark.parametrize(
    ("mime", "name", "expected"),
    [
        ("video/mp4", "clip.bin", True),
        ("video/webm; codecs=vp9", "clip.bin", True),
        (None, "clip.MP4", True),
        ("video/x-matroska", "clip.mkv", False),
        ("image/gif", "clip.gif", False),
    ],
)
def test_browser_playback_scope(mime, name, expected):
    assert is_browser_playable_video(mime, name) is expected


def test_browser_video_mime_type_uses_container_extension():
    assert browser_video_mime_type("clip.mp4", "application/octet-stream") == "video/mp4"
    assert browser_video_mime_type("clip.webm", None) == "video/webm"
    assert browser_video_mime_type("clip.bin", "video/mp4; codecs=avc1") == "video/mp4"


def test_video_probe_and_atomic_posters(tmp_path):
    source = _make_video(tmp_path / "source")
    inspection = inspect_video(source)
    assert (inspection.width, inspection.height) == (160, 90)
    assert inspection.duration == pytest.approx(1.0, abs=0.2)

    output = tmp_path / "library"
    result = render_video_derivatives(source, output, "source")
    assert result.error is None
    assert result.thumbnail_path == output / "source.thumbnail.webp"
    assert result.poster_path == output / "source.poster.webp"
    assert result.thumbnail_path.stat().st_size > 0
    assert result.poster_path.stat().st_size > 0
    assert not list(output.glob(".*.tmp.webp"))

    first_mtime = result.poster_path.stat().st_mtime_ns
    repeated = render_video_derivatives(source, output, "source")
    assert repeated.poster_path.stat().st_mtime_ns == first_mtime


def test_two_derivatives_share_one_ffmpeg_decode(tmp_path, monkeypatch):
    from app.services import media_assets

    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    commands = []

    monkeypatch.setattr(
        media_assets,
        "inspect_video",
        lambda *_args, **_kwargs: VideoInspection(1920, 1080, 10.0, "h264", "mp4"),
    )

    def fake_run(command, **_kwargs):
        commands.append(command)
        for index, argument in enumerate(command):
            if argument == "-y":
                Path(command[index + 1]).write_bytes(b"webp")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(media_assets.subprocess, "run", fake_run)
    result = render_video_derivatives(source, tmp_path / "library", "source")

    assert result.error is None
    assert len(commands) == 1
    filter_graph = commands[0][commands[0].index("-filter_complex") + 1]
    assert "split=2" in filter_graph
    assert filter_graph.count("scale=") == 2
    assert result.thumbnail_path and result.thumbnail_path.read_bytes() == b"webp"
    assert result.poster_path and result.poster_path.read_bytes() == b"webp"


def test_webm_probe_and_posters(tmp_path):
    source = _make_video(tmp_path / "source", ".webm")
    result = render_video_derivatives(source, tmp_path / "library", "source")
    assert result.error is None
    assert result.inspection
    assert (result.inspection.width, result.inspection.height) == (160, 90)
    assert result.thumbnail_path and result.thumbnail_path.exists()
    assert result.poster_path and result.poster_path.exists()


def test_representative_frame_falls_back_to_first_decodable_frame(tmp_path, monkeypatch):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"fixture")
    target = tmp_path / "poster.webp"
    seeks: list[float] = []

    def fake_run(command, **_kwargs):
        seek = float(command[command.index("-ss") + 1])
        seeks.append(seek)
        if seek == 0:
            Path(command[-1]).write_bytes(b"webp")
            return SimpleNamespace(returncode=0, stderr="")
        return SimpleNamespace(returncode=1, stderr="no frame")

    monkeypatch.setattr(subprocess, "run", fake_run)
    error = _render_video_frame(
        source,
        target,
        seek_seconds=1.5,
        max_size=400,
        timeout_seconds=5,
    )
    assert error is None
    assert seeks == [1.5, 0.0]
    assert target.read_bytes() == b"webp"


def test_ffprobe_timeout_is_reported_without_raising(tmp_path, monkeypatch):
    source = tmp_path / "slow.mp4"
    source.write_bytes(b"fixture")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ffprobe", 1)

    monkeypatch.setattr(subprocess, "run", timeout)
    result = render_video_derivatives(source, tmp_path / "library", "slow")
    assert result.inspection is None
    assert result.error and "timed out" in result.error


def test_corrupt_video_returns_diagnostic_without_outputs(tmp_path):
    source = tmp_path / "broken.mp4"
    source.write_bytes(b"not-a-video")
    result = render_video_derivatives(source, tmp_path / "library", "broken")
    assert result.inspection is None
    assert result.error and "ffprobe failed" in result.error
    assert result.thumbnail_path is None
    assert result.poster_path is None
