"""Pure fields and fingerprints used by search index documents."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import unicodedata
from typing import Any, Iterable

def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _timestamp(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _thumbnail_projection(
    work: Any,
    preview_asset_ids: list[str],
    dimensions: dict[str, tuple[int | None, int | None]],
) -> tuple[str | None, int | None, int | None]:
    selected = (
        str(work.thumbnail_asset_id)
        if getattr(work, "thumbnail_asset_id", None)
        else (preview_asset_ids[0] if preview_asset_ids else None)
    )
    width, height = dimensions.get(selected, (None, None)) if selected else (None, None)
    return (
        selected,
        int(width) if width is not None and int(width) > 0 else None,
        int(height) if height is not None and int(height) > 0 else None,
    )


def _with_projection_hash(document: dict[str, Any], *, version: int = 1) -> dict[str, Any]:
    """Attach a deterministic projection fingerprint for sampled audits."""

    projected = dict(document)
    projected["projection_version"] = version
    payload = json.dumps(
        projected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    projected["projection_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return projected


def _normalize_reference_name(value: str) -> str:
    """Match PostgreSQL's reference-list normalization in search documents."""

    return unicodedata.normalize("NFKC", value).casefold()


def _alias_projection_fields(aliases: Iterable[Any]) -> dict[str, list[Any]]:
    name_kinds = {"name", "other_name"}
    ordered = sorted(
        aliases,
        key=lambda item: (
            not bool(item.is_current),
            0 if item.kind == "name" else 1 if item.kind == "other_name" else 2,
            str(item.source),
            str(item.normalized_value),
        ),
    )
    fields: dict[str, list[Any]] = {
        "alias_names_current": [],
        "alias_names_historical": [],
        "alias_identities_current": [],
        "alias_identities_historical": [],
        "alias_records": [],
    }
    seen: dict[str, set[str]] = {
        key: set() for key in fields if key != "alias_records"
    }
    for item in ordered:
        family = "names" if item.kind in name_kinds else "identities"
        suffix = "current" if item.is_current else "historical"
        field = f"alias_{family}_{suffix}"
        normalized = str(item.normalized_value)
        if normalized not in seen[field]:
            seen[field].add(normalized)
            fields[field].append(str(item.value))
        fields["alias_records"].append(
            {
                "value": str(item.value),
                "normalized_value": normalized,
                "source": str(item.source),
                "kind": str(item.kind),
                "is_current": bool(item.is_current),
            }
        )
    return fields


def _merge_alias_projection_fields(
    projections: Iterable[dict[str, list[Any]]],
) -> dict[str, list[Any]]:
    merged = _alias_projection_fields(())
    seen: dict[str, set[str]] = {key: set() for key in merged if key != "alias_records"}
    for projection in projections:
        for key in seen:
            for value in projection.get(key, []):
                normalized = _normalize_reference_name(str(value))
                if normalized not in seen[key]:
                    seen[key].add(normalized)
                    merged[key].append(value)
        merged["alias_records"].extend(projection.get("alias_records", []))
    return merged


