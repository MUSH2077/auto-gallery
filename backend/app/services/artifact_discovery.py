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


class _ParentInventory:
    __slots__ = ("all_assets", "by_stem")

    def __init__(self, assets: Iterable[Path]) -> None:
        ordered = tuple(sorted(assets, key=lambda path: (path.stem, path.suffix)))
        self.all_assets = ordered
        by_stem: dict[str, list[Path]] = defaultdict(list)
        for asset in ordered:
            by_stem[asset.stem].append(asset)
        self.by_stem = {stem: tuple(paths) for stem, paths in by_stem.items()}


class _DirectoryInventory:
    """Scan every touched parent directory at most once per metadata batch."""

    __slots__ = ("_parents", "_restricted")

    def __init__(self, *, restricted: bool = False) -> None:
        self._parents: dict[Path, _ParentInventory] = {}
        self._restricted = restricted

    @classmethod
    def from_allowed_paths(cls, allowed_paths: set[Path]) -> "_DirectoryInventory":
        """Build the parent/stem map directly from one staging delta."""

        inventory = cls(restricted=True)
        assets_by_parent: dict[Path, list[Path]] = defaultdict(list)
        parents: set[Path] = set()
        for path in allowed_paths:
            parents.add(path.parent)
            if path.suffix.lower() in ASSET_EXTENSIONS:
                assets_by_parent[path.parent].append(path)
        for parent in parents:
            inventory._parents[parent] = _ParentInventory(assets_by_parent.get(parent, ()))
        return inventory

    def parent(self, path: Path) -> _ParentInventory:
        inventory = self._parents.get(path)
        if inventory is None:
            if self._restricted:
                inventory = _ParentInventory(())
            else:
                inventory = _ParentInventory(
                    candidate
                    for candidate in path.iterdir()
                    if candidate.is_file() and candidate.suffix.lower() in ASSET_EXTENSIONS
                )
            self._parents[path] = inventory
        return inventory

    def matching_stems(self, parents: set[Path], stems: set[str]) -> set[Path]:
        matches: set[Path] = set()
        for parent in parents:
            inventory = self.parent(parent)
            for stem in stems:
                matches.update(inventory.by_stem.get(stem, ()))
        return matches


class _InventoryPool:
    """Share full and staging-restricted inventories across every work group."""

    __slots__ = ("full", "restricted")

    def __init__(self) -> None:
        self.full = _DirectoryInventory()
        self.restricted: dict[int, tuple[set[Path], _DirectoryInventory]] = {}

    def select(self, allowed_paths: set[Path] | None) -> _DirectoryInventory:
        if allowed_paths is None:
            return self.full
        key = id(allowed_paths)
        cached = self.restricted.get(key)
        if cached is not None and cached[0] is allowed_paths:
            return cached[1]
        inventory = _DirectoryInventory.from_allowed_paths(allowed_paths)
        self.restricted[key] = (allowed_paths, inventory)
        return inventory


class _MetadataGroup(list[MetadataItem]):
    """List-compatible group carrying the shared parent inventory."""

    __slots__ = ("inventory_pool",)

    def __init__(self, items: Iterable[MetadataItem], inventory_pool: _InventoryPool) -> None:
        super().__init__(items)
        self.inventory_pool = inventory_pool


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

    inventory_pool = _InventoryPool()
    return {
        source_work_id: _MetadataGroup(items, inventory_pool)
        for source_work_id, items in groups.items()
    }, invalid


def media_files_for_group(
    items: list[MetadataItem],
    source_work_id: str,
    *,
    allowed_paths: set[Path] | None = None,
) -> list[Path]:
    """Return only media files belonging to one metadata work group.

    Metadata postprocessors normally produce ``image.ext`` + ``image.json``.
    This exact stem match handles flat/date directories without cross-linking
    neighboring works. Per-work directories such as manual uploads may instead
    contain a single ``metadata.json``; those safely fall back to all media when
    the directory itself is named after the work ID. ``allowed_paths`` narrows
    matching to one download staging delta without changing legacy callers.
    """
    if not items:
        return []

    parents = {path.parent for path, _ in items}
    stems = {path.stem for path, _ in items}
    inventory_pool = getattr(items, "inventory_pool", None)
    if isinstance(inventory_pool, _InventoryPool):
        inventory = inventory_pool.select(allowed_paths)
    elif allowed_paths is not None:
        inventory = _DirectoryInventory.from_allowed_paths(allowed_paths)
    else:
        # Plain lists from older/direct callers retain the public contract and
        # still scan each of their parents only once for this lookup.
        inventory = _DirectoryInventory()
    exact_candidates = inventory.matching_stems(parents, stems)
    if allowed_paths is not None:
        exact_candidates.intersection_update(allowed_paths)
    exact = sorted(
        exact_candidates,
        key=lambda path: (str(path.parent), path.stem, path.suffix),
    )
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
    indexed_candidates = inventory.matching_stems(parents, indexed_stems)
    if allowed_paths is not None:
        indexed_candidates.intersection_update(allowed_paths)
    indexed = sorted(
        (
            candidate
            for candidate in indexed_candidates
            if (
                candidate.stem not in expected_extensions
                or candidate.suffix.lower() == expected_extensions[candidate.stem]
            )
        ),
        key=lambda path: (str(path.parent), path.stem, path.suffix),
    )
    if indexed:
        return indexed

    if len(parents) == 1:
        parent = next(iter(parents))
        if parent.name == str(source_work_id):
            assets = inventory.parent(parent).all_assets
            if allowed_paths is not None:
                return [path for path in assets if path in allowed_paths]
            return list(assets)

    return []
