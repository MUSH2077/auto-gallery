"""Pixel and fingerprint compatibility for bounded large-image processing."""

import imagehash
import numpy as np
import pytest
from PIL import Image, ImageOps, PngImagePlugin

from app.services import work_import
from app.services.asset_reconciliation import _normalized_pixels, structural_similarity, SSIM_MIN_SCORE
from app.services.thumbnail import inspect_and_generate_thumbnail
from app.services.work_import import WorkImportService, _png_phash_sample
from app.services.perceptual_hash import phash as current_phash


def pixels(mode="RGB"):
    rng = np.random.default_rng(710)
    return rng.integers(0, 256, (173, 193, len(mode)), dtype=np.uint8)


@pytest.mark.parametrize("mode", ["RGB", "RGBA"])
def test_streamed_png_sample_is_pixel_exact_and_never_loads_pillow_rgb(tmp_path, monkeypatch, mode):
    path = tmp_path / "source.png"
    Image.fromarray(pixels(mode), mode).save(path)
    with Image.open(path) as original:
        expected_sample = original.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
        expected_hash = current_phash(original)
    with Image.open(path) as original:
        sample = _png_phash_sample(path, original)
        assert sample.tobytes() == expected_sample.tobytes()
        sample.close()

    monkeypatch.setattr(work_import, "PNG_PHASH_STREAM_MIN_PIXELS", 0)
    def forbidden_load(*_args, **_kwargs):
        raise AssertionError("The streaming path must not decode the full Pillow RGB bitmap")
    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", forbidden_load)
    assert WorkImportService.phash(path) == expected_hash


@pytest.mark.parametrize("mode", ["L", "P", "I;16"])
def test_other_png_modes_keep_original_hash(tmp_path, monkeypatch, mode):
    path = tmp_path / "other.png"
    if mode == "I;16":
        Image.fromarray(np.arange(4096, dtype=np.uint16).reshape(64, 64)).save(path)
    else:
        Image.fromarray(pixels()).convert(mode).save(path)
    with Image.open(path) as original:
        expected = current_phash(original)
        assert _png_phash_sample(path, original) is None
    monkeypatch.setattr(work_import, "PNG_PHASH_STREAM_MIN_PIXELS", 0)
    assert WorkImportService.phash(path) == expected


def test_interlaced_png_keeps_original_hash(tmp_path, monkeypatch):
    import pyvips
    data = pixels()
    path = tmp_path / "interlaced.png"
    pyvips.Image.new_from_memory(data.data, 193, 173, 3, "uchar").pngsave(str(path), interlace=True)
    with Image.open(path) as original:
        expected = current_phash(original)
        assert _png_phash_sample(path, original) is None
    monkeypatch.setattr(work_import, "PNG_PHASH_STREAM_MIN_PIXELS", 0)
    assert WorkImportService.phash(path) == expected


def test_streaming_path_preserves_pillow_bomb_limit(tmp_path, monkeypatch):
    path = tmp_path / "source.png"
    Image.fromarray(pixels()).save(path)
    monkeypatch.setattr(work_import, "PNG_PHASH_STREAM_MIN_PIXELS", 0)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(Image.DecompressionBombError):
        WorkImportService.phash(path)


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P"])
@pytest.mark.parametrize("orientation", [1, 6, 8])
def test_ssim_normalization_remains_pixel_exact(tmp_path, mode, orientation):
    path = tmp_path / "oriented.png"
    source = Image.fromarray(pixels()).convert(mode)
    exif = source.getexif()
    exif[274] = orientation
    source.save(path, exif=exif)
    with Image.open(path) as opened:
        expected = np.asarray(ImageOps.exif_transpose(opened).convert("RGB")
                              .resize((256, 256), Image.Resampling.LANCZOS), dtype=np.float64) / 255.0
    assert np.array_equal(_normalized_pixels(path), expected)


def test_ssim_keeps_resize_compression_positive_and_negative_decisions(tmp_path):
    source = Image.new("RGB", (768, 512), "white")
    source.paste("navy", (0, 0, 384, 256))
    original = tmp_path / "original.png"
    resized = tmp_path / "resized.jpg"
    different = tmp_path / "different.png"
    source.save(original)
    source.resize((384, 256), Image.Resampling.LANCZOS).save(resized, quality=95)
    Image.new("RGB", source.size, "black").save(different)
    assert structural_similarity(original, original) == pytest.approx(1.0)
    assert structural_similarity(original, resized) >= SSIM_MIN_SCORE
    assert structural_similarity(original, different) < SSIM_MIN_SCORE


@pytest.mark.parametrize("suffix,mode,orientation", [(".jpg", "RGB", 1), (".jpg", "RGB", 6),
                                                    (".png", "RGBA", 1), (".png", "RGBA", 8)])
def test_file_thumbnail_keeps_dimensions_orientation_and_alpha(tmp_path, suffix, mode, orientation):
    import pyvips
    path = tmp_path / ("source" + suffix)
    image = Image.new(mode, (900, 600), (30, 90, 180, 80) if mode == "RGBA" else (30, 90, 180))
    exif = image.getexif()
    exif[274] = orientation
    image.save(path, exif=exif)
    old = pyvips.Image.new_from_file(str(path), access="sequential").thumbnail_image(400, height=400000)
    result, width, height = inspect_and_generate_thumbnail(str(path), tmp_path / "library")
    assert (width, height) == (900, 600)
    with Image.open(result) as thumbnail:
        assert thumbnail.size == (old.width, old.height)
        assert ("A" in thumbnail.mode) == (mode == "RGBA")
    assert not list((tmp_path / "library").glob(".*.tmp.webp"))


def test_truncated_png_cannot_produce_a_partial_fingerprint(tmp_path, monkeypatch):
    import pyvips
    path = tmp_path / "truncated.png"
    Image.fromarray(pixels()).save(path)
    path.write_bytes(path.read_bytes()[:-100])
    monkeypatch.setattr(work_import, "PNG_PHASH_STREAM_MIN_PIXELS", 0)
    with pytest.raises((OSError, pyvips.Error)):
        WorkImportService.phash(path)
