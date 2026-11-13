"""The 64-bit pHash transform using NumPy real FFTs only."""

from functools import lru_cache

SAMPLE_EDGE = 32
HASH_EDGE = 8


def grayscale_sample(image):
    """Keep ImageHash's grayscale conversion and direct LANCZOS sampling."""
    from PIL import Image

    gray = image if image.mode == "L" else image.convert("L")
    try:
        return gray.resize((SAMPLE_EDGE, SAMPLE_EDGE), Image.Resampling.LANCZOS)
    finally:
        if gray is not image:
            gray.close()


@lru_cache(maxsize=1)
def _phase():
    import numpy as np

    return np.exp(-1j * np.pi * np.arange(HASH_EDGE) / (2 * SAMPLE_EDGE))


def phash_from_sample(pixels: bytes) -> str:
    """DCT-II on a 32x32 L sample, with the same DC/median/bit ordering.

    An even extension followed by a real FFT and phase correction gives the
    unnormalized DCT-II used by ImageHash. Only its first eight frequencies in
    each direction are needed; NumPy is already used by SSIM.
    """
    import numpy as np

    values = np.frombuffer(pixels, dtype=np.uint8).reshape(SAMPLE_EDGE, SAMPLE_EDGE)
    vertical = np.fft.rfft(np.concatenate((values, values[::-1]), axis=0), axis=0)
    vertical = (vertical[:HASH_EDGE] * _phase()[:, None]).real
    horizontal = np.fft.rfft(
        np.concatenate((vertical, vertical[:, ::-1]), axis=1), axis=1,
    )
    coefficients = (horizontal[:, :HASH_EDGE] * _phase()[None, :]).real
    bits = coefficients > np.median(coefficients)
    return np.packbits(bits, axis=None).tobytes().hex()


def phash(image) -> str:
    with grayscale_sample(image) as sample:
        return phash_from_sample(sample.tobytes())
