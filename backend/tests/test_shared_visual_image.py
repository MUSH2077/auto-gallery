"""Behavior checks for bounded shared visual samples and task reuse."""
import asyncio
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import numpy as np
import pytest
import imagehash
from PIL import Image, ImageDraw, ImageOps, PngImagePlugin

from app.services import visual_image, media_derivatives
from app.services.asset_reconciliation import structural_similarity, AssetReconciliation
from app.services.work_import import WorkImportService
from app.services.perceptual_hash import phash as current_phash


def drawing(size=(1600, 1200)):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((size[0]//10, size[1]//10, size[0]*7//10, size[1]*6//10), fill="navy")
    draw.ellipse((size[0]//3, size[1]//3, size[0]*9//10, size[1]*9//10), fill="orange")
    draw.line((0, size[1], size[0], 0), fill="red", width=23)
    return image


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P"])
@pytest.mark.parametrize("orientation", [1, 2, 3, 4, 5, 6, 7, 8])
def test_small_work_pixels_keep_display_orientation(tmp_path, mode, orientation):
    path = tmp_path / "oriented.png"
    source = drawing((320, 240)).convert(mode)
    exif = source.getexif()
    exif[274] = orientation
    source.save(path, exif=exif)
    with Image.open(path) as opened:
        expected = ImageOps.exif_transpose(opened).convert("RGB")
    with visual_image.working_image(path) as work:
        assert work.original_size == (320, 240)
        assert work.display_size == expected.size
        assert np.array_equal(np.asarray(work.pixels.convert("RGB")), np.asarray(expected))
        assert len(work.sample()) == visual_image.SAMPLE_BYTES


def test_png_with_trailing_exif_does_not_require_full_pillow_decode(tmp_path, monkeypatch):
    path = tmp_path / "trailing.png"
    image = drawing((1600, 1200))
    image.save(path)
    exif = image.getexif()
    exif[274] = 6
    payload = exif.tobytes()[6:]
    kind = b"eXIf"
    chunk = struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    data = path.read_bytes()
    path.write_bytes(data[:-12] + chunk + data[-12:])
    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load",
                        lambda *_a, **_k: pytest.fail("full Pillow PNG decode"))
    with visual_image.working_image(path) as work:
        assert work.display_size == (1200, 1600)
        assert max(work.pixels.size) <= 1024
    assert visual_image.display_dimensions(path) == (1200, 1600)


def test_shared_samples_keep_positive_and_negative_classification(tmp_path):
    original, variant, other = [tmp_path / name for name in ("original.png", "variant.jpg", "other.png")]
    drawing().save(original)
    drawing().resize((800, 600), Image.Resampling.LANCZOS).save(variant, quality=95)
    Image.new("RGB", (1600, 1200), "black").save(other)
    for shared in (False, True):
        assert structural_similarity(original, variant, shared=shared) >= 0.98
        assert structural_similarity(original, other, shared=shared) < 0.98
    with visual_image.working_image(original) as left, visual_image.working_image(variant) as right:
        assert (int(left.phash(), 16) ^ int(right.phash(), 16)).bit_count() <= 4


def test_analysis_slots_reject_other_owner_changed_source_and_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(visual_image.settings, "library_root", str(tmp_path / "library"))
    path = tmp_path / "source.png"
    drawing().save(path)
    asset_id = UUID(int=1)
    sha = WorkImportService.sha256(path)
    with visual_image.working_image(path) as work:
        visual_image.save_sample(path, asset_id, sha, work)
        expected = work.sample()
    assert visual_image.read_sample(path, asset_id, sha)[0] == expected
    assert visual_image.read_sample(path, UUID(int=1 + visual_image.CACHE_SLOTS), sha) is None
    assert visual_image.read_sample(path, asset_id, "0"*64) is None
    assert visual_image.owned_sample_relative(asset_id)
    assert visual_image.owned_sample_relative(UUID(int=1 + visual_image.CACHE_SLOTS)) is None
    import os
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
    assert visual_image.read_sample(path, asset_id, sha) is None


def test_task_cache_is_bounded_reuses_samples_and_recovers_missing_disk_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(visual_image.settings, "library_root", str(tmp_path / "library"))
    paths = [tmp_path / f"{i}.png" for i in range(3)]
    for path in paths:
        drawing((320, 240)).save(path)
    cache = visual_image.SampleCache(limit_bytes=2 * visual_image.SAMPLE_BYTES)
    calls = []
    original = visual_image.analysis_pixels
    def record(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(visual_image, "analysis_pixels", record)
    asset_id = uuid4()
    sha = WorkImportService.sha256(paths[0])
    cache.get(paths[0], asset_id=asset_id, sha256=sha)
    cache.get(paths[0], asset_id=asset_id, sha256=sha)
    assert len(calls) == 1
    assert visual_image.SampleCache().get(paths[0], asset_id=asset_id, sha256=sha)[0]
    assert len(calls) == 1
    visual_image._cache_path(asset_id).unlink()
    visual_image.SampleCache().get(paths[0], asset_id=asset_id, sha256=sha)
    assert len(calls) == 2
    for path in paths[1:]:
        cache.get(path)
    assert cache.bytes == 2 * visual_image.SAMPLE_BYTES
    assert len(cache.entries) == 2
    cache.clear()
    assert cache.bytes == 0


def test_media_reuses_work_pixels_and_recomputes_wrong_phash_version(tmp_path, monkeypatch):
    downloads, library = tmp_path / "downloads", tmp_path / "library"
    downloads.mkdir()
    library.mkdir()
    monkeypatch.setattr(media_derivatives.settings, "download_root", str(downloads))
    monkeypatch.setattr(media_derivatives.settings, "library_root", str(library))
    path = downloads / "image.png"
    drawing().save(path)
    stat = path.stat()
    asset_id = uuid4()
    descriptor = {"asset_id": asset_id, "file_path": path.name, "file_name": path.name,
                  "mime_type": "image/png", "library_dir": str(library / "work")}
    request = {"source_size": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
               "algorithm_version": media_derivatives.MEDIA_ALGORITHM_VERSION,
               "requested": {"thumbnail": True, "dimensions": True, "sha256": True, "phash": True}}
    calls = []
    monkeypatch.setattr(visual_image, "SHARED_MIN_PIXELS", 0)
    original = media_derivatives.prepare_visual_work
    def record(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(media_derivatives, "prepare_visual_work", record)
    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load",
                        lambda *_a, **_k: pytest.fail("full Pillow PNG decode"))
    values = media_derivatives._derive_sync(descriptor, request)
    assert len(calls) == 1
    assert values["phash_version"] == visual_image.PHASH_VERSION
    with Image.open(library / values["thumb_sm_path"]) as thumb:
        assert thumb.size == (400, 300)
    assert visual_image.read_sample(path, asset_id, values["sha256"])
    descriptor.update(values)
    assert "phash" not in media_derivatives._derive_sync(descriptor, request)
    assert len(calls) == 1
    descriptor["phash_version"] = "imagehash-phash-v1"
    assert media_derivatives._derive_sync(descriptor, request)["phash_version"] == visual_image.PHASH_VERSION
    assert len(calls) == 2


def test_elongated_image_thumbnail_is_not_upscaled_from_work_image(tmp_path):
    path = tmp_path / "long.png"
    drawing((800, 8000)).save(path)
    from app.services.thumbnail import inspect_and_generate_thumbnail
    with visual_image.working_image(path) as work:
        result, width, height = inspect_and_generate_thumbnail(str(path), tmp_path / "library", working=work)
    with Image.open(result) as thumbnail:
        assert thumbnail.size == (400, 4000)
    assert (width, height) == (800, 8000)


def test_invalid_sample_is_rebuilt_and_cache_write_failure_keeps_ssim(tmp_path, monkeypatch):
    monkeypatch.setattr(visual_image.settings, "library_root", str(tmp_path / "library"))
    path = tmp_path / "source.png"
    drawing((320, 240)).save(path)
    asset_id = uuid4()
    sha = WorkImportService.sha256(path)
    target = visual_image._cache_path(asset_id)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"truncated")
    monkeypatch.setattr(visual_image, "_write_sample", lambda *_a: (_ for _ in ()).throw(OSError("full disk")))
    assert structural_similarity(path, path, shared=True, left_id=asset_id, right_id=asset_id,
                                 left_sha=sha, right_sha=sha) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_oriented_pair_uses_display_ratio_and_reuses_versioned_samples(tmp_path, monkeypatch):
    downloads, library = tmp_path / "downloads", tmp_path / "library"
    downloads.mkdir()
    monkeypatch.setattr(visual_image.settings, "download_root", str(downloads))
    monkeypatch.setattr(visual_image.settings, "library_root", str(library))
    source = drawing((800, 600))
    paths = [downloads / "upright.png", downloads / "oriented.png"]
    source.save(paths[0])
    rotated = source.transpose(Image.Transpose.ROTATE_90)
    exif = rotated.getexif()
    exif[274] = 6
    rotated.save(paths[1], exif=exif)
    assets = []
    for path in paths:
        with visual_image.working_image(path) as work:
            assets.append(SimpleNamespace(id=uuid4(), sha256=WorkImportService.sha256(path),
                          phash=work.phash(), phash_version=visual_image.PHASH_VERSION,
                          file_size=path.stat().st_size, width=work.original_size[0],
                          height=work.original_size[1], mime_type="image/png", file_path=path.name))
    service = AssetReconciliation(None)
    from app.services.asset_reconciliation import DedupPolicy
    service._policy_value = DedupPolicy()
    async def metadata(*_args):
        return {"score": 0}
    monkeypatch.setattr(service, "_metadata_evidence", metadata)
    facts = await service._evaluate_pair(*assets)
    assert facts["hard_gate_passed"]
    assert facts["aspect_ratio_delta"] == 0
    assert facts["ssim_score"] == pytest.approx(1)
    assert service._samples.bytes == 2 * visual_image.SAMPLE_BYTES
    # A version mismatch must not be papered over with a visually equal sample.
    assets[1].phash_version = "imagehash-phash-v1"
    assert not (await service._evaluate_pair(*assets))["hard_gate_passed"]


@pytest.mark.asyncio
async def test_upgrade_enqueues_only_available_legacy_images(tmp_path, monkeypatch):
    from app.services import visual_fingerprint_rebuild as rebuild
    from sqlalchemy.dialects import postgresql
    path = tmp_path / "available.png"
    drawing((320, 240)).save(path)
    available, unavailable = uuid4(), uuid4()
    async def batches(_size):
        yield [SimpleNamespace(id=available, file_path=path.name),
               SimpleNamespace(id=unavailable, file_path="missing.png")]
    monkeypatch.setattr(rebuild, "legacy_assets", batches)
    monkeypatch.setattr(rebuild, "_safe_source_path", lambda relative: tmp_path / relative)
    recorded = []
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def execute(self, query): recorded.append(query)
        async def commit(self): pass
    monkeypatch.setattr(rebuild, "async_session", Session)
    result = await rebuild.enqueue_upgrade(2)
    assert result == {"queued": 1, "unavailable": 1}
    compiled = recorded[1].compile(dialect=postgresql.dialect())
    assert available in compiled.params.values()
    assert unavailable not in compiled.params.values()
    assert {"sha256": True, "phash": True} in compiled.params.values()

def test_old_intent_cannot_publish_new_hash_with_old_media_label(tmp_path, monkeypatch):
    path = tmp_path / "source.png"
    drawing((320, 240)).save(path)
    monkeypatch.setattr(media_derivatives.settings, "download_root", str(tmp_path))
    stat = path.stat()
    request = {"source_size": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
               "algorithm_version": "media-v1", "requested": {"phash": True}}
    descriptor = {"file_path": path.name, "library_dir": str(tmp_path / "library")}
    with pytest.raises(RuntimeError, match="algorithm upgrade"):
        media_derivatives._derive_sync(descriptor, request)


@pytest.mark.parametrize("mode", ["L", "P", "I;16"])
def test_unmeasured_png_layout_keeps_direct_pixels_and_hash(tmp_path, monkeypatch, mode):
    path = tmp_path / "other.png"
    if mode == "I;16":
        Image.fromarray(np.arange(4096, dtype=np.uint16).reshape(64, 64)).save(path)
    else:
        drawing((320, 240)).convert(mode).save(path)
    monkeypatch.setattr(visual_image, "SHARED_MIN_PIXELS", 0)
    monkeypatch.setattr(visual_image, "working_image", lambda *_a: pytest.fail("unsupported streaming layout"))
    assert visual_image.prepare_visual_work(path) is None
    assert WorkImportService.phash(path, oriented=True) == WorkImportService.phash(path)
    with Image.open(path) as opened:
        with opened.convert("RGB").resize((256, 256), Image.Resampling.LANCZOS) as expected:
            assert visual_image.analysis_pixels(path)[0] == expected.tobytes()

def test_elongated_png_keeps_low_ram_direct_grayscale_hash(tmp_path, monkeypatch):
    from app.services import work_import
    path = tmp_path / "tall.png"
    drawing((192, 1280)).save(path)
    expected = WorkImportService.phash(path)
    monkeypatch.setattr(visual_image, "SHARED_MIN_PIXELS", 0)
    monkeypatch.setattr(work_import, "PNG_PHASH_STREAM_MIN_PIXELS", 0)
    assert visual_image.prepare_visual_work(path) is None
    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load",
                        lambda *_a, **_k: pytest.fail("full Pillow PNG decode"))
    assert WorkImportService.phash(path, oriented=True) == expected

def test_small_missing_card_shares_one_png_decode_without_native_cache(tmp_path, monkeypatch):
    import builtins
    from app.services import thumbnail

    downloads, library = tmp_path / "downloads", tmp_path / "library"
    downloads.mkdir()
    library.mkdir()
    monkeypatch.setattr(media_derivatives.settings, "download_root", str(downloads))
    monkeypatch.setattr(media_derivatives.settings, "library_root", str(library))
    path = downloads / "small.png"
    drawing((800, 600)).save(path)
    stat = path.stat()
    asset_id = uuid4()
    descriptor = {"asset_id": asset_id, "file_path": path.name, "file_name": path.name,
                  "mime_type": "image/png", "library_dir": str(library / "work")}
    request = {"source_size": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
               "algorithm_version": media_derivatives.MEDIA_ALGORITHM_VERSION,
               "requested": {"thumbnail": True, "dimensions": True, "sha256": True, "phash": True}}
    with Image.open(path) as image:
        expected = current_phash(image)
    loads = []
    original_load = PngImagePlugin.PngImageFile.load
    def observed_load(image, *args, **kwargs):
        if image._im is None:
            loads.append(image.filename)
        return original_load(image, *args, **kwargs)
    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", observed_load)
    original_import = builtins.__import__
    def without_vips(name, *args, **kwargs):
        if name == "pyvips":
            pytest.fail("A bounded shared PNG should not initialize libvips")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", without_vips)
    values = media_derivatives._derive_sync(descriptor, request)
    assert len(loads) == 1
    assert values["phash"] == expected
    assert (values["width"], values["height"]) == (800, 600)
    assert not visual_image._cache_path(asset_id).exists()
    with Image.open(library / values["thumb_sm_path"]) as thumb:
        assert thumb.size == (400, 300)


@pytest.mark.parametrize("suffix,mode,size", [
    (".jpg", "CMYK", (800, 600)),
    (".png", "L", (800, 600)),
    (".png", "RGB", (3000, 3000)),
    (".png", "RGB", (280, 210)),
])
def test_small_shared_policy_keeps_unmeasured_layouts_on_existing_path(tmp_path, suffix, mode, size):
    path = tmp_path / ("source" + suffix)
    with Image.new(mode, size) as image:
        image.save(path)
    assert visual_image.prepare_small_visual_work(path) is None

def test_rgb_png_color_key_keeps_thumbnail_transparency(tmp_path, monkeypatch):
    downloads, library = tmp_path / "downloads", tmp_path / "library"
    downloads.mkdir()
    library.mkdir()
    monkeypatch.setattr(media_derivatives.settings, "download_root", str(downloads))
    monkeypatch.setattr(media_derivatives.settings, "library_root", str(library))
    path = downloads / "color-key.png"
    with Image.new("RGB", (800, 600), "red") as image:
        image.paste("navy", (200, 150, 600, 450))
        image.save(path, transparency=(255, 0, 0))
    assert visual_image.prepare_small_visual_work(path) is None
    stat = path.stat()
    descriptor = {"asset_id": uuid4(), "file_path": path.name, "file_name": path.name,
                  "mime_type": "image/png", "library_dir": str(library / "work")}
    request = {"source_size": stat.st_size, "source_mtime_ns": stat.st_mtime_ns,
               "algorithm_version": media_derivatives.MEDIA_ALGORITHM_VERSION,
               "requested": {"thumbnail": True, "dimensions": True, "sha256": True, "phash": True}}
    values = media_derivatives._derive_sync(descriptor, request)
    with Image.open(library / values["thumb_sm_path"]) as thumb:
        assert thumb.mode == "RGBA"
        assert thumb.getpixel((0, 0))[3] == 0
        assert thumb.getpixel((200, 150))[3] == 255


@pytest.mark.parametrize("format,mode", [("JPEG", "RGB"), ("PNG", "RGB"), ("PNG", "RGBA")])
@pytest.mark.parametrize("orientation", [1, 2, 3, 4, 5, 6, 7, 8])
def test_bounded_card_reuses_pixels_without_changing_direct_phash(tmp_path, format, mode, orientation):
    path = tmp_path / ("card.jpg" if format == "JPEG" else "card.png")
    source = drawing((900, 600)).convert(mode)
    if mode == "RGBA":
        source.putalpha(96)
    exif = source.getexif()
    exif[274] = orientation
    from PIL import ImageCms
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    source.save(path, exif=exif, icc_profile=profile)
    expected = WorkImportService.phash(path, oriented=True)
    from app.services.thumbnail import inspect_and_generate_thumbnail
    with visual_image.prepare_small_visual_work(path) as work:
        assert work.original_size == (900, 600)
        assert work.display_size == ((600, 900) if orientation in (5, 6, 7, 8) else (900, 600))
        assert work.phash() == expected
        thumbnail, width, height = inspect_and_generate_thumbnail(str(path), tmp_path / "cards", working=work)
    with Image.open(thumbnail) as card:
        assert card.info["icc_profile"] == profile
        assert card.width == 400
        assert card.height == round(work.display_size[1] * 400 / work.display_size[0])
        assert ("A" in card.getbands()) == (mode == "RGBA")
    assert (width, height) == (900, 600)
    with pytest.raises(ValueError):
        work.pixels.getpixel((0, 0))


def test_bounded_card_keeps_pixel_limit_and_special_format_fallbacks(tmp_path, monkeypatch):
    monkeypatch.setattr(visual_image, "SMALL_WORK_MAX_PIXELS", 600 * 400)
    for size, expected in [((600, 400), True), ((601, 400), False)]:
        path = tmp_path / f"{size[0]}.jpg"
        drawing(size).save(path)
        work = visual_image.prepare_small_visual_work(path)
        assert (work is not None) is expected
        if work is not None:
            work.pixels.close()
    for name, image, kwargs in [
        ("palette.png", drawing((600, 400)).convert("P"), {}),
        ("transparent.png", drawing((600, 400)), {"transparency": (255, 255, 255)}),
        ("cmyk.jpg", drawing((600, 400)).convert("CMYK"), {}),
        ("animated.png", drawing((600, 400)).convert("RGBA"), {"save_all": True, "append_images": [Image.new("RGBA", (600, 400), "red")]})
    ]:
        path = tmp_path / name
        image.save(path, **kwargs)
        assert visual_image.prepare_small_visual_work(path) is None


def test_jpeg_card_task_does_not_load_second_native_image_stack(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "card.jpg"
    drawing((900, 600)).save(path)
    code = """
import sys
from pathlib import Path
from app.config import settings
from app.services.media_derivatives import _derive_sync, MEDIA_ALGORITHM_VERSION
path = Path(sys.argv[1]); library = Path(sys.argv[2]); settings.download_root = str(path.parent); settings.library_root = str(library)
stat = path.stat()
value = _derive_sync({'file_path':path.name,'file_name':path.name,'mime_type':'image/jpeg','library_dir':str(library)},
    {'source_size':stat.st_size,'source_mtime_ns':stat.st_mtime_ns,'algorithm_version':MEDIA_ALGORITHM_VERSION,
     'requested':{'thumbnail':True,'dimensions':True,'sha256':True,'phash':True}})
assert value['phash'] and value['thumb_sm_path'] and (value['width'],value['height']) == (900,600)
assert 'pyvips' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code, str(path), str(tmp_path / "library")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
