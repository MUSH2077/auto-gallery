"""Verify pHash mathematics, sampling and version isolation."""

import subprocess
import sys

import imagehash
import numpy as np
import pytest
from PIL import Image, ImageOps

from app.services.perceptual_hash import phash, phash_from_sample


@pytest.mark.parametrize("value", [0, 1, 127, 128, 254, 255])
def test_constant_sample_matches_deployed_hash(value):
    with Image.new("L", (32, 32), value) as image:
        assert phash(image) == str(imagehash.phash(image))


def test_structured_and_random_samples_match_deployed_hash():
    rng = np.random.default_rng(739)
    x, y = np.indices((32, 32))
    samples = [x * 8, y * 8, x * 4 + y * 4]
    for step in [1, 2, 3, 4, 8, 16]:
        samples.extend([((x // step) % 2) * 255,
                        ((x // step + y // step) % 2) * 255])
    # Cover repeated rows/columns, mirror symmetry, and sparse logos as well
    # as natural-like samples; these expose zero/tied low-frequency coefficients.
    for _ in range(100):
        a = rng.integers(0, 256, (32, 32), dtype=np.uint8)
        samples.extend([a, np.tile(a[0], (32, 1)), np.tile(a[:, :1], (1, 32)),
                        np.concatenate((a[:, :16], a[:, :16][:, ::-1]), axis=1),
                        np.where(a > 250, 255, 0)])
    for background, foreground in [(0, 255), (255, 0), (127, 128)]:
        for position in range(1024):
            values = np.full((32, 32), background, np.uint8)
            values.flat[position] = foreground
            samples.append(values)
    for values in samples:
        with Image.fromarray(values.astype(np.uint8), "L") as image:
            from scipy.fftpack import dct
            reference = dct(dct(values.astype(np.uint8), axis=0), axis=1)[:8, :8]
            median = np.median(reference)
            # Cross-implementation rounding may change exactly tied bits. Away
            # from those ties the new version must preserve the DCT-II decision.
            stable = np.abs(reference - median) > 1e-6
            actual = np.unpackbits(np.frombuffer(bytes.fromhex(phash_from_sample(image.tobytes())),
                                                dtype=np.uint8)).reshape(8, 8).astype(bool)
            assert np.array_equal(actual[stable], (reference > median)[stable])


@pytest.mark.parametrize("mode", ["RGB", "RGBA", "L", "P"])
@pytest.mark.parametrize("orientation", [1, 2, 3, 4, 5, 6, 7, 8])
def test_direct_sampling_and_orientation_match_deployed_hash(mode, orientation):
    rng = np.random.default_rng(18)
    pixels = rng.integers(0, 256, (93, 157, 3), dtype=np.uint8)
    with Image.fromarray(pixels, "RGB").convert(mode) as image:
        image.getexif()[274] = orientation
        ImageOps.exif_transpose(image, in_place=True)
        assert phash(image) == str(imagehash.phash(image))


def test_fresh_fingerprints_never_load_scipy():
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys,numpy as np; from PIL import Image; "
         "from app.services.perceptual_hash import phash; "
         "image=Image.fromarray(np.random.default_rng(739).integers(0,256,(32,32),dtype=np.uint8)); "
         "assert phash(image); "
         "assert not any(k=='scipy' or k.startswith('scipy.') for k in sys.modules)"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_new_fingerprint_version_is_not_compared_with_previous_values():
    from types import SimpleNamespace
    from app.services.asset_reconciliation import versioned_phash_distance
    from app.services.visual_image import PHASH_VERSION

    previous = SimpleNamespace(phash="8000000000000000",
                               phash_version="imagehash-phash-oriented-adaptive-v2")
    current = SimpleNamespace(phash=previous.phash, phash_version=PHASH_VERSION)
    assert versioned_phash_distance(previous.phash, current.phash, previous.phash_version, current.phash_version) is None
