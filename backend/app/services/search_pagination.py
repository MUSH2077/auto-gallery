"""Pure search sort, stable cursor, and shuffle-ring rules.

The public SearchService continues to own database and Meilisearch execution.
Keep cursor wire formats here so both backends share deterministic boundaries.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any
from uuid import UUID

from app.models import Work
from app.services.search_language import SearchQuery

MEILI_SHUFFLE_FIELDS = (
    "shuffle_high",
    "shuffle_low",
    "shuffle_id_0",
    "shuffle_id_1",
    "shuffle_id_2",
    "shuffle_id_3",
)

def _sql_sort_spec(query: SearchQuery, model) -> tuple[Any, str, bool]:
    selected = query.values("sort")
    value = selected[0] if selected else "created-desc"
    field = value.rsplit("-", 1)[0]
    attribute = {
        "created": "created_at",
        "updated": "updated_at",
        "posted": "posted_at",
        "heat": "heat_score",
        "title": "title",
        "name": "name",
        "last-sync": "last_synced_at",
    }.get(field, "created_at")
    column = getattr(model, attribute, model.created_at)
    direction_asc = value.endswith("-asc")
    return column, attribute, direction_asc


def _apply_sql_sort(stmt, query: SearchQuery, model, *, reverse: bool = False):
    column, _attribute, direction_asc = _sql_sort_spec(query, model)
    if reverse:
        direction_asc = not direction_asc
    ordered = column.asc() if direction_asc else column.desc()
    # A deterministic UUID tie-breaker keeps offset pages stable when titles or
    # timestamps collide. Nullable fields need explicit NULL placement for
    # cursor boundaries; nonnullable fields retain the existing index order.
    if column.nullable:
        ordered = ordered.nulls_first() if reverse else ordered.nulls_last()
    identity_order = model.id.asc() if direction_asc else model.id.desc()
    return stmt.order_by(ordered, identity_order)


def _encode_work_cursor(
    query: SearchQuery,
    work: Work,
    *,
    seek: str,
    force_sfw: bool,
) -> str:
    _column, attribute, _ascending = _sql_sort_spec(query, Work)
    boundary = getattr(work, attribute)
    if isinstance(boundary, datetime):
        boundary = boundary.isoformat()
    payload = {
        "v": 1,
        "q": hashlib.sha256(query.canonical.encode("utf-8")).hexdigest()[:16],
        "sfw": bool(force_sfw),
        "sort": (query.values("sort") or ("created-desc",))[0],
        "seek": seek,
        "value": boundary,
        "id": str(work.id),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def _decode_work_cursor(
    cursor: str,
    query: SearchQuery,
    *,
    force_sfw: bool,
) -> tuple[str, Any, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        expected_query = hashlib.sha256(query.canonical.encode("utf-8")).hexdigest()[:16]
        expected_sort = (query.values("sort") or ("created-desc",))[0]
        if (
            payload.get("v") != 1
            or payload.get("q") != expected_query
            or bool(payload.get("sfw")) != bool(force_sfw)
            or payload.get("sort") != expected_sort
            or payload.get("seek") not in {"after", "before"}
        ):
            raise ValueError
        identity = UUID(str(payload["id"]))
        _column, attribute, _ascending = _sql_sort_spec(query, Work)
        value = payload.get("value")
        if value is not None and attribute in {"created_at", "updated_at", "posted_at"}:
            value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        elif value is not None and attribute == "heat_score":
            value = float(value)
        elif value is not None:
            value = str(value)
        return str(payload["seek"]), value, identity
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid or stale works cursor") from exc


SHUFFLE_RING_SIZE = 1 << 63


@dataclass(frozen=True)
class RandomWorkCursor:
    seek: str
    key: int
    identity: UUID
    phase: int


def _shuffle_ring_start(seed: int) -> int:
    """Map a public uint32 seed to a uniformly distributed hash-ring point."""

    if not 0 <= seed <= 0xFFFFFFFF:
        raise ValueError("Random seed must be an unsigned 32-bit integer")
    digest = hashlib.blake2b(
        seed.to_bytes(4, "big", signed=False),
        digest_size=8,
        person=b"ag-random",
    ).digest()
    return int.from_bytes(digest, "big") & (SHUFFLE_RING_SIZE - 1)


def _meili_shuffle_key(value: int) -> str:
    """Keep 63-bit hash order exact across Meilisearch's JSON number boundary."""

    if not 0 <= int(value) < SHUFFLE_RING_SIZE:
        raise ValueError("Shuffle key is outside the stable hash ring")
    return f"{int(value):019d}"


def _meili_shuffle_parts(value: int, identity: UUID) -> dict[str, int]:
    key = int(value)
    if not 0 <= key < SHUFFLE_RING_SIZE:
        raise ValueError("Shuffle key is outside the stable hash ring")
    identity_value = identity.int
    return {
        "shuffle_high": key >> 31,
        "shuffle_low": key & ((1 << 31) - 1),
        "shuffle_id_0": (identity_value >> 96) & 0xFFFFFFFF,
        "shuffle_id_1": (identity_value >> 64) & 0xFFFFFFFF,
        "shuffle_id_2": (identity_value >> 32) & 0xFFFFFFFF,
        "shuffle_id_3": identity_value & 0xFFFFFFFF,
    }


def _meili_tuple_comparison(
    fields: tuple[str, ...],
    values: tuple[int, ...],
    operator: str,
    *,
    inclusive_last: bool = False,
) -> str:
    clauses = []
    for index, (field, value) in enumerate(zip(fields, values, strict=True)):
        prefix = " AND ".join(
            f"{fields[position]} = {values[position]}"
            for position in range(index)
        )
        comparison = (
            f"{field} {operator}= {value}"
            if inclusive_last and index == len(fields) - 1
            else f"{field} {operator} {value}"
        )
        clauses.append(f"({prefix} AND {comparison})" if prefix else comparison)
    return "(" + " OR ".join(clauses) + ")"


def _meili_random_phase_filter(
    base_filter: str | None,
    *,
    start: int,
    phase: int,
    boundary: RandomWorkCursor | None = None,
    reverse: bool = False,
) -> str:
    if phase not in {0, 1}:
        raise ValueError("Invalid random ring phase")
    start_parts = _meili_shuffle_parts(start, UUID(int=0))
    key_fields = MEILI_SHUFFLE_FIELDS[:2]
    key_values = tuple(start_parts[field] for field in key_fields)
    parts = [_meili_tuple_comparison(
        key_fields,
        key_values,
        ">" if phase == 0 else "<",
        inclusive_last=phase == 0,
    )]
    if boundary is not None:
        boundary_parts = _meili_shuffle_parts(boundary.key, boundary.identity)
        operator = "<" if reverse else ">"
        parts.append(_meili_tuple_comparison(
            MEILI_SHUFFLE_FIELDS,
            tuple(boundary_parts[field] for field in MEILI_SHUFFLE_FIELDS),
            operator,
        ))
    if base_filter:
        parts.insert(0, base_filter)
    return " AND ".join(f"({part})" for part in parts)


def _encode_random_work_cursor(
    query: SearchQuery,
    work: Work,
    *,
    seek: str,
    force_sfw: bool,
    seed: int,
    phase: int,
) -> str:
    if seek not in {"after", "before"} or phase not in {0, 1}:
        raise ValueError("Invalid random cursor boundary")
    payload = {
        "v": 2,
        "q": hashlib.sha256(query.canonical.encode("utf-8")).hexdigest()[:16],
        "sfw": bool(force_sfw),
        "sort": "random",
        "seed": seed,
        "phase": phase,
        "seek": seek,
        "key": int(work.shuffle_key),
        "id": str(work.id),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def _decode_random_work_cursor(
    cursor: str,
    query: SearchQuery,
    *,
    force_sfw: bool,
    seed: int,
) -> RandomWorkCursor:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        expected_query = hashlib.sha256(query.canonical.encode("utf-8")).hexdigest()[:16]
        if (
            payload.get("v") != 2
            or payload.get("q") != expected_query
            or bool(payload.get("sfw")) != bool(force_sfw)
            or payload.get("sort") != "random"
            or payload.get("seed") != seed
            or payload.get("phase") not in {0, 1}
            or payload.get("seek") not in {"after", "before"}
        ):
            raise ValueError
        key = int(payload["key"])
        if not 0 <= key < SHUFFLE_RING_SIZE:
            raise ValueError
        return RandomWorkCursor(
            seek=str(payload["seek"]),
            key=key,
            identity=UUID(str(payload["id"])),
            phase=int(payload["phase"]),
        )
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid or stale works cursor") from exc


