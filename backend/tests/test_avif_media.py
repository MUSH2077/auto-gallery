"""Real codec coverage for downloaded AVIF assets and derivatives."""

import pytest
from PIL import AvifImagePlugin, Image, UnidentifiedImageError

from app.services import media_derivatives
from app.services.artifact_discovery import ASSET_EXTENSIONS
from app.services.artifact_ledger import artifact_row
from app.services.asset_reconciliation import IMAGE_MIME_TYPES, structural_similarity
from app.services.image_utils import can_compute_phash, can_generate_thumbnail, get_mime_type
from app.services.media_assets import media_kind
from app.services.work_import import WorkImportService


def _source(tmp_path, monkeypatch, *, mode="RGB", frames=1):
    downloads = tmp_path / "downloads"
    library = tmp_path / "library"
    downloads.mkdir()
    library.mkdir()
    monkeypatch.setattr(media_derivatives.settings, "download_root", str(downloads))
    monkeypatch.setattr(media_derivatives.settings, "library_root", str(library))
    path = downloads / "source.AVIF"
    image = Image.new(mode, (512, 384), (30, 80, 160, 100) if mode == "RGBA" else (30, 80, 160))
    image.paste((200, 60, 40, 150) if mode == "RGBA" else (200, 60, 40), (0, 0, 230, 170))
    options = {"format": "AVIF", "max_threads": 1, "speed": 10}
    if frames > 1:
        options.update(save_all=True, append_images=[Image.new(mode, image.size, "green")], duration=100)
    image.save(path, **options)
    return path, library


def _derive(path, library):
    stat = path.stat()
    descriptor = {
        "file_path": path.name, "file_name": path.name, "mime_type": get_mime_type(path.suffix),
        "library_dir": str(library / "work"),
    }
    request = {
        "requested": {"thumbnail": True, "dimensions": True, "sha256": True, "phash": True, "dedup": True},
        "source_size": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
        "algorithm_version": media_derivatives.MEDIA_ALGORITHM_VERSION,
    }
    return media_derivatives._derive_sync(descriptor, request)


@pytest.mark.parametrize("suffix", [".avif", ".AVIF"])
def test_avif_discovery_ledger_and_media_capabilities(tmp_path, suffix):
    root = tmp_path / "downloads"
    path = root / "danbooru" / "artist" / f"123_None{suffix}"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"ledger fixture")
    row = artifact_row(path, root, source_work_id="123")
    assert suffix.lower() in ASSET_EXTENSIONS
    assert row["artifact_type"] == "image"
    assert row["source_work_id"] == "123"
    assert get_mime_type(suffix) == "image/avif"
    assert can_generate_thumbnail(suffix)
    assert can_compute_phash(suffix)
    assert media_kind(None, path.name) == "image"
    assert media_kind("image/avif", "asset.bin") == "image"
    assert "image/avif" in IMAGE_MIME_TYPES


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_static_avif_produces_real_webp_dimensions_and_fingerprints(tmp_path, monkeypatch, mode):
    path, library = _source(tmp_path, monkeypatch, mode=mode)
    values = _derive(path, library)
    assert (values["width"], values["height"]) == (512, 384)
    assert len(values["sha256"]) == 64
    assert len(values["phash"]) == 16
    assert values["phash_version"] == media_derivatives.PHASH_ALGORITHM_VERSION
    with Image.open(library / values["thumb_sm_path"]) as thumbnail:
        assert thumbnail.format == "WEBP"
        assert thumbnail.size == (400, 300)
        assert ("A" in thumbnail.mode) == (mode == "RGBA")
    assert AvifImagePlugin._get_default_max_threads() == 1
    assert not list(library.rglob("*.tmp.webp"))


def test_avif_sequence_completes_with_first_frame_preview_and_exact_fingerprint(tmp_path, monkeypatch):
    path, library = _source(tmp_path, monkeypatch, frames=2)
    values = _derive(path, library)
    assert (values["width"], values["height"]) == (512, 384)
    assert len(values["sha256"]) == 64
    assert values["phash"] is None
    assert values["phash_version"] is None
    with Image.open(library / values["thumb_sm_path"]) as thumbnail:
        assert thumbnail.size == (400, 300)
        assert thumbnail.n_frames == 1


def test_corrupt_avif_cannot_complete_requested_derivatives(tmp_path, monkeypatch):
    path, library = _source(tmp_path, monkeypatch)
    path.write_bytes(b"invalid avif bytes")
    with pytest.raises(UnidentifiedImageError):
        _derive(path, library)
    assert not list(library.rglob("*.webp"))


def test_static_avif_can_be_compared_with_other_image_formats(tmp_path, monkeypatch):
    path, _library = _source(tmp_path, monkeypatch)
    png = tmp_path / "decoded.png"
    from app.services.image_utils import open_pillow_image

    with open_pillow_image(path) as image:
        image.save(png)
    assert structural_similarity(path, png) == pytest.approx(1.0)


def test_missing_jpeg_phash_still_fails_requested_result_validation(tmp_path, monkeypatch):
    path, library = _source(tmp_path, monkeypatch)
    jpeg = path.with_suffix(".jpg")
    with Image.open(path) as image:
        image.save(jpeg)
    monkeypatch.setattr(WorkImportService, "phash", lambda _path: None)
    with pytest.raises(RuntimeError, match="requested perceptual hash was not produced"):
        _derive(jpeg, library)
