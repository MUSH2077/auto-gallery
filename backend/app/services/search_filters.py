"""Pure Meilisearch qualifier, filter, and sort compilation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timezone
import json
from typing import Any

from app.services.search_language import (
    HAS_TARGETS,
    IS_TARGETS,
    SearchQualifier,
    SearchQuery,
    SearchTarget,
)
from app.services.search_pagination import MEILI_SHUFFLE_FIELDS
from app.services.source_search_identity import ParsedSourceURL

@dataclass(frozen=True)
class ResolvedSourceURL:
    parsed: ParsedSourceURL
    work_ids: tuple[str, ...] = ()
    creator_ids: tuple[str, ...] = ()
    repository_ids: tuple[str, ...] = ()
    subscription_ids: tuple[str, ...] = ()

    def ids_for(self, target: SearchTarget) -> tuple[str, ...]:
        return {
            "works": self.work_ids,
            "creators": self.creator_ids,
            "repositories": self.repository_ids,
            "subscriptions": self.subscription_ids,
        }.get(target, ())


DEFAULT_SORT = {
    "works": "created_ts:desc",
    "creators": "name_sort:asc",
    "tags": "usage_count:desc",
    "repositories": "updated_ts:desc",
    "subscriptions": "name_sort:asc",
}

SORT_FIELD = {
    "heat-desc": ("heat_score", "desc"),
    "posted-desc": ("posted_ts", "desc"),
    "posted-asc": ("posted_ts", "asc"),
    "created-desc": ("created_ts", "desc"),
    "created-asc": ("created_ts", "asc"),
    "updated-desc": ("updated_ts", "desc"),
    "updated-asc": ("updated_ts", "asc"),
    "name-asc": ("name_sort", "asc"),
    "name-desc": ("name_sort", "desc"),
    "usage-desc": ("usage_count", "desc"),
    "last-sync-desc": ("synced_ts", "desc"),
    "last-sync-asc": ("synced_ts", "asc"),
    "title-asc": ("title", "asc"),
    "title-desc": ("title", "desc"),
}

MEILI_FIELD = {
    "works": {
        "repo": "repository_ids",
        "creator": "creator_ids",
        "tag": "tags",
        "source": "sources",
        "uid": "source_creator_keys",
        "pid": "source_work_keys",
        "posted": "posted_ts",
        "created": "created_ts",
        "updated": "updated_ts",
    },
    "creators": {
        "creator": "id",
        "source": "sources",
        "uid": "source_creator_keys",
        "created": "created_ts",
        "updated": "updated_ts",
    },
    "tags": {
        "tag": "normalized_name",
        "created": "created_ts",
        "updated": "updated_ts",
    },
    "repositories": {
        "repo": "id",
        "creator": "creator_id",
        "source": "source",
        "uid": "source_creator_keys",
        "created": "created_ts",
        "updated": "updated_ts",
        "synced": "synced_ts",
    },
    "subscriptions": {
        "repo": "repository_ids",
        "creator": "creator_id",
        "source": "sources",
        "uid": "source_creator_keys",
        "created": "created_ts",
        "updated": "updated_ts",
        "synced": "synced_ts",
    },
}

IS_FIELD: dict[str, dict[str, tuple[str, bool | str]]] = {
    "works": {
        "favorite": ("is_favorite", True),
        "nsfw": ("is_nsfw", True),
        "sfw": ("is_nsfw", False),
        "ai": ("is_ai_generated", True),
        "human": ("is_ai_generated", False),
        "visible": ("visibility", "visible"),
        "trashed": ("visibility", "trashed"),
    },
    "creators": {
        "favorite": ("is_favorite", True),
        "active": ("is_active", True),
        "inactive": ("is_active", False),
    },
    "repositories": {
        "enabled": ("is_enabled", True),
        "disabled": ("is_enabled", False),
        "auth-ok": ("auth_state", "healthy"),
        "auth-error": ("auth_state", "unhealthy"),
    },
    "subscriptions": {
        "active": ("is_active", True),
        "inactive": ("is_active", False),
        "sync-enabled": ("sync_enabled", True),
        "sync-disabled": ("sync_enabled", False),
        "never-synced": ("never_synced", True),
    },
}

HAS_FIELD = {
    "works": {
        "tags": "has_tags",
        "description": "has_description",
        "multiple-assets": "has_multiple_assets",
        "image": "has_image",
        "animation": "has_animation",
        "video": "has_video",
    },
    "creators": {
        "subscription": "has_subscription",
        "repository": "has_repository",
        "danbooru": "has_danbooru",
    },
    "repositories": {
        "last-sync": "has_last_sync",
        "source-creator-id": "has_source_creator_id",
    },
    "subscriptions": {
        "last-sync": "has_last_sync",
    },
}


def _meili_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _date_bounds(raw: str) -> tuple[str, int, int | None]:
    operator = "="
    value = raw
    for candidate in ("<=", ">=", "<", ">", "="):
        if raw.startswith(candidate):
            operator = candidate
            value = raw[len(candidate):]
            break
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    start = int(parsed.timestamp())
    if "T" not in value.upper():
        end = int(datetime.combine(parsed.date(), time.max, tzinfo=parsed.tzinfo).timestamp())
    else:
        end = None
    return operator, start, end


def _date_expression(field: str, raw: str) -> str:
    operator, start, end = _date_bounds(raw)
    if operator == "=" and end is not None:
        return f"({field} >= {start} AND {field} <= {end})"
    return f"{field} {operator} {start}"


def _grouped_qualifiers(query: SearchQuery, target: SearchTarget) -> dict[tuple[str, bool], list[SearchQualifier]]:
    grouped: dict[tuple[str, bool], list[SearchQualifier]] = defaultdict(list)
    for token in query.qualifiers:
        if token.key in {"type", "sort"}:
            continue
        if token.key == "is" and target not in IS_TARGETS.get(token.value, frozenset()):
            continue
        if token.key == "has" and target not in HAS_TARGETS.get(token.value, frozenset()):
            continue
        grouped[(token.key, token.negated)].append(token)
    return grouped


def _resolved_value(token: SearchQualifier, resolved: dict[tuple[str, str], Any]) -> str:
    value = resolved.get((token.key, token.value), token.value)
    return value if isinstance(value, str) else token.value


def _resolved_source_url(
    token: SearchQualifier,
    resolved: dict[tuple[str, str], Any],
) -> ResolvedSourceURL | None:
    value = resolved.get((token.key, token.value))
    return value if isinstance(value, ResolvedSourceURL) else None


def _compile_meili_filter(
    query: SearchQuery,
    target: SearchTarget,
    resolved: dict[tuple[str, str], Any],
    *,
    force_sfw: bool,
    identity_field: str = "id",
) -> str | None:
    parts: list[str] = []
    fields = MEILI_FIELD[target]
    for (key, negated), tokens in _grouped_qualifiers(query, target).items():
        expressions: list[str] = []
        if key == "is":
            for token in tokens:
                field, value = IS_FIELD[target][token.value]
                expressions.append(f"{field} {'!=' if negated else '='} {_meili_literal(value)}")
        elif key == "has":
            for token in tokens:
                field = HAS_FIELD[target][token.value]
                expressions.append(f"{field} = {'false' if negated else 'true'}")
        elif key in {"posted", "created", "updated", "synced"}:
            for token in tokens:
                expression = _date_expression(fields[key], token.value)
                expressions.append(f"NOT ({expression})" if negated else expression)
        elif key == "url":
            for token in tokens:
                source_url = _resolved_source_url(token, resolved)
                identities = source_url.ids_for(target) if source_url else ()
                if identities:
                    identity_expression = " OR ".join(
                        f"{identity_field} = {_meili_literal(identity)}" for identity in identities
                    )
                    expression = f"({identity_expression})"
                else:
                    expression = f'{identity_field} = "__source_url_no_match__"'
                expressions.append(f"NOT ({expression})" if negated else expression)
        elif key in fields:
            field = fields[key]
            for token in tokens:
                value = _resolved_value(token, resolved)
                expressions.append(f"{field} {'!=' if negated else '='} {_meili_literal(value)}")
        if expressions:
            # Positive values of the same qualifier are alternatives. Negative
            # values all have to be absent.
            parts.append(f"({' AND '.join(expressions)})" if negated else f"({' OR '.join(expressions)})")

    if target == "works":
        visibility_values = {
            token.value
            for token in query.qualifiers
            if token.key == "is" and not token.negated and token.value in {"visible", "trashed"}
        }
        if not visibility_values:
            parts.append('visibility = "visible"')
        if force_sfw:
            parts.append("is_nsfw = false")
    return " AND ".join(parts) or None


def _meili_sort(query: SearchQuery, target: SearchTarget) -> list[str] | None:
    selected = query.values("sort")
    if selected and selected[0] != "relevance":
        if selected[0] == "heat-desc":
            return ["heat_available:desc", "heat_score:desc", "id:desc"]
        if selected[0] == "random":
            return [f"{field}:asc" for field in MEILI_SHUFFLE_FIELDS]
        field, direction = SORT_FIELD[selected[0]]
        order = [f"{field}:{direction}"]
        if target in {"works", "creators", "repositories", "subscriptions"} and field != "id":
            order.append(f"id:{direction}")
        return order
    if query.terms or selected == ("relevance",):
        return None
    default = DEFAULT_SORT.get(target)
    if not default:
        return None
    order = [default]
    if target in {"works", "creators", "repositories", "subscriptions"}:
        direction = default.rsplit(":", 1)[-1]
        order.append(f"id:{direction}")
    return order


def _matching_strategy(target: SearchTarget) -> str:
    return "last" if target == "works" else "all"


