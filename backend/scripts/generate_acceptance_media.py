#!/usr/bin/env python3
"""Generate deterministic, non-production media edge cases for acceptance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

from PIL import Image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--works", type=int, default=200)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    marker = root.parent / ".auto-gallery-test-root"
    if not marker.is_file() or root.parent not in root.parents:
        parser.error("root must be directly below a marked acceptance directory")
    root.mkdir(parents=True, exist_ok=True)
    flat = root / "flat"
    flat.mkdir(exist_ok=True)
    for number in range(max(1, args.works)):
        stem = f"fixture-{number:06d}"
        width, height = (320, 240) if number % 9 else (320, 2400)
        image = Image.new("RGB", (width, height), ((number * 17) % 255, 80, 140))
        image.save(flat / f"{stem}.jpg", quality=82)
        (flat / f"{stem}.json").write_text(
            json.dumps(
                {
                    "id": stem,
                    "title": f"Fixture {number}",
                    "author": "acceptance",
                    "tags": [f"tag-{value}" for value in range(number % 14)],
                    "source": f"http://provider-stub:8099/{stem}",
                }
            ),
            encoding="utf-8",
        )
    first = flat / "fixture-000000.jpg"
    (flat / "duplicate.jpg").write_bytes(first.read_bytes())
    (flat / "corrupt.jpg").write_bytes(b"not-an-image")
    (flat / "invalid.json").write_text("{invalid", encoding="utf-8")
    video = flat / "fixture-video.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=12",
            "-t", "2", "-pix_fmt", "yuv420p", str(video),
        ],
        check=True,
    )
    print(json.dumps({"root": str(root), "works": args.works, "video": str(video)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
