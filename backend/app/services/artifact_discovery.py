"""Provider-aware discovery of gallery-dl metadata and media artifacts.

gallery-dl directory layouts are configurable, so the third path component is
not reliably a work ID (Twitter installations commonly use a date directory).
The provider metadata is the source of truth for work identity; matching media
is selected by the metadata filename stem when a directory contains multiple
works.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


ASSET_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".zip", ".mp4", ".webm",
}

MetadataItem = tuple[Path, dict[str, Any]]


def group_metadata_by_work(
    provider: Any,
    metadata_paths: Iterable[Path],
) -> tuple[dict[str, list[MetadataItem]], list[Path]]:
    """Parse metadata files and group them by provider-native work ID."""
    groups: dict[str, list[MetadataItem]] = defaultdict(list)
    invalid: list[Path] = []

    for metadata_path in sorted(metadata_paths):
        try:
            with metadata_path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
            work = provider.parse_work_source(raw)
            source_work_id = str(work.get("source_work_id") or "").strip()
            if not source_work_id:
                invalid.append(metadata_path)
                continue
            groups[source_work_id].append((metadata_path, raw))
        except Exception:
            invalid.append(metadata_path)

    return dict(groups), invalid


def media_files_for_group(items: list[MetadataItem], source_work_id: str) -> list[Path]:
    """Return only media files belonging to one metadata work group.

    Metadata postprocessors normally produce ``image.ext`` + ``image.json``.
    This exact stem match handles flat/date directories without cross-linking
    neighboring works. Per-work directories such as manual uploads may instead
    contain a single ``metadata.json``; those safely fall back to all media when
    the directory itself is named after the work ID.
    """
    if not items:
        return []

    parents = {path.parent for path, _ in items}
    stems = {path.stem for path, _ in items}
    exact = sorted({
        candidate
        for parent in parents
        for candidate in parent.iterdir()
        if (
            candidate.is_file()
            and candidate.suffix.lower() in ASSET_EXTENSIONS
            and candidate.stem in stems
        )
    }, key=lambda path: (str(path.parent), path.stem, path.suffix))
    if exact:
        return exact

    # Some gallery-dl configurations name metadata after the remote media key
    # (for example ``GRFAFgKbMAA2qqC.json``) but name the downloaded file after
    # the parent post (``1806295396614095134_1.jpg``). ``num`` is the stable
    # per-post asset index emitted in each metadata document, so the pair
    # (parsed work ID, num) remains exact even when multiple posts share a date
    # directory.
    indexed_stems: set[str] = set()
    expected_extensions: dict[str, str] = {}
    for _, raw in items:
        num = raw.get("num")
        if num is None:
            continue
        stem = f"{source_work_id}_{num}"
        indexed_stems.add(stem)
        extension = str(raw.get("extension") or "").strip().lower()
        if extension:
            expected_extensions[stem] = f".{extension.lstrip('.')}"
    indexed = sorted({
        candidate
        for parent in parents
        for candidate in parent.iterdir()
        if (
            candidate.is_file()
            and candidate.suffix.lower() in ASSET_EXTENSIONS
            and candidate.stem in indexed_stems
            and (
                candidate.stem not in expected_extensions
                or candidate.suffix.lower() == expected_extensions[candidate.stem]
            )
        )
    }, key=lambda path: (str(path.parent), path.stem, path.suffix))
    if indexed:
        return indexed

    if len(parents) == 1:
        parent = next(iter(parents))
        if parent.name == str(source_work_id):
            return sorted(
                (
                    candidate
                    for candidate in parent.iterdir()
                    if candidate.is_file() and candidate.suffix.lower() in ASSET_EXTENSIONS
                ),
                key=lambda path: (path.stem, path.suffix),
            )

    return []
