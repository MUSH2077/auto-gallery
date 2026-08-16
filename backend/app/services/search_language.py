"""Canonical compound-search language for every first-party search surface.

The public seam of this module is deliberately small:

``parse_search_query(query, scope)`` turns user input into a validated,
canonical AST.  Search adapters consume that AST and never parse raw strings.
``compose_search_query`` applies UI filter changes through the same parser so
the web client does not need to grow a second grammar.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import re
from typing import Iterable, Literal

from app.services.source_search_identity import (
    IDENTITY_SOURCES,
    parse_source_identity,
    parse_source_url,
)


SearchScope = Literal[
    "global",
    "works",
    "creators",
    "tags",
    "repositories",
    "subscriptions",
    "tasks",
    "scheduler",
    "creator-picker",
]

SearchTarget = Literal[
    "works",
    "creators",
    "tags",
    "repositories",
    "subscriptions",
    "tasks",
    "scheduler",
]


SCOPE_TARGETS: dict[str, tuple[SearchTarget, ...]] = {
    "global": ("works", "creators", "tags", "repositories", "subscriptions"),
    "works": ("works",),
    "creators": ("creators",),
    "tags": ("tags",),
    "repositories": ("repositories",),
    "subscriptions": ("subscriptions",),
    "tasks": ("tasks",),
    "scheduler": ("scheduler",),
    "creator-picker": ("creators",),
}

TYPE_TARGETS: dict[str, SearchTarget] = {
    "work": "works",
    "creator": "creators",
    "tag": "tags",
    "repo": "repositories",
    "subscription": "subscriptions",
}

TYPE_ALIASES = {
    "works": "work",
    "creators": "creator",
    "tags": "tag",
    "repository": "repo",
    "repositories": "repo",
    "repos": "repo",
    "subscriptions": "subscription",
}

QUALIFIER_KEYS = frozenset({
    "type",
    "repo",
    "creator",
    "tag",
    "source",
    "is",
    "has",
    "status",
    "kind",
    "posted",
    "created",
    "updated",
    "synced",
    "sort",
    "uid",
    "pid",
    "url",
})

NEGATABLE_KEYS = frozenset({
    "repo", "creator", "tag", "source", "is", "has", "uid", "pid", "url",
})

IS_TARGETS: dict[str, frozenset[SearchTarget]] = {
    "favorite": frozenset({"works", "creators"}),
    "nsfw": frozenset({"works"}),
    "sfw": frozenset({"works"}),
    "ai": frozenset({"works"}),
    "human": frozenset({"works"}),
    "visible": frozenset({"works"}),
    "trashed": frozenset({"works"}),
    "active": frozenset({"creators", "subscriptions"}),
    "inactive": frozenset({"creators", "subscriptions"}),
    "enabled": frozenset({"repositories"}),
    "disabled": frozenset({"repositories", "scheduler"}),
    "auth-ok": frozenset({"repositories"}),
    "auth-error": frozenset({"repositories"}),
    "sync-enabled": frozenset({"subscriptions"}),
    "sync-disabled": frozenset({"subscriptions"}),
    "never-synced": frozenset({"subscriptions"}),
    "due": frozenset({"scheduler"}),
    "blocked": frozenset({"scheduler"}),
    "waiting": frozenset({"scheduler"}),
    "manual": frozenset({"scheduler"}),
}

HAS_TARGETS: dict[str, frozenset[SearchTarget]] = {
    "tags": frozenset({"works"}),
    "description": frozenset({"works"}),
    "multiple-assets": frozenset({"works"}),
    "image": frozenset({"works"}),
    "animation": frozenset({"works"}),
    "video": frozenset({"works"}),
    "subscription": frozenset({"creators"}),
    "repository": frozenset({"creators"}),
    "danbooru": frozenset({"creators"}),
    "last-sync": frozenset({"repositories", "subscriptions"}),
    "source-creator-id": frozenset({"repositories"}),
}

QUALIFIER_TARGETS: dict[str, frozenset[SearchTarget]] = {
    "repo": frozenset({"works", "repositories", "subscriptions", "tasks", "scheduler"}),
    "creator": frozenset({"works", "creators", "repositories", "subscriptions", "tasks", "scheduler"}),
    "tag": frozenset({"works", "tags"}),
    "source": frozenset({"works", "repositories", "subscriptions", "tasks", "scheduler"}),
    "status": frozenset({"tasks"}),
    "kind": frozenset({"tasks"}),
    "posted": frozenset({"works"}),
    "created": frozenset({"works", "creators", "tags", "repositories", "subscriptions", "tasks"}),
    "updated": frozenset({"works", "creators", "tags", "repositories", "subscriptions", "tasks"}),
    "synced": frozenset({"repositories", "subscriptions"}),
    "uid": frozenset({"works", "creators", "repositories", "subscriptions"}),
    "pid": frozenset({"works"}),
    "url": frozenset({"works", "creators", "repositories", "subscriptions"}),
}


QUALIFIER_HELP: dict[str, tuple[str, str, str]] = {
    "type": ("search.qualifier.type", "type:work", "Limit results to an object type."),
    "repo": ("search.qualifier.repo", "repo:pixiv/123", "Filter by subscription repository."),
    "creator": ("search.qualifier.creator", 'creator:\"name\"', "Filter by local creator name or ID."),
    "tag": ("search.qualifier.tag", "tag:landscape", "Filter by an exact normalized tag."),
    "source": ("search.qualifier.source", "source:pixiv", "Filter by source platform."),
    "is": ("search.qualifier.is", "is:favorite", "Filter by object state."),
    "has": ("search.qualifier.has", "has:video", "Filter by content, media, or related records."),
    "status": ("search.qualifier.status", "status:failed", "Filter by task status."),
    "kind": ("search.qualifier.kind", "kind:download", "Filter by task kind."),
    "posted": ("search.qualifier.posted", "posted:>=2026-01-01", "Filter by work publication date."),
    "created": ("search.qualifier.created", "created:>=2026-01-01", "Filter by local creation date."),
    "updated": ("search.qualifier.updated", "updated:<2026-08-01", "Filter by local update date."),
    "synced": ("search.qualifier.synced", "synced:>=2026-08-01", "Filter by most recent sync date."),
    "sort": ("search.qualifier.sort", "sort:updated-desc", "Choose the result ordering."),
    "uid": ("search.qualifier.uid", "uid:pixiv/123", "Find an exact source creator ID."),
    "pid": ("search.qualifier.pid", "pid:pixiv/456", "Find an exact source work ID."),
    "url": (
        "search.qualifier.url",
        'url:\"https://www.pixiv.net/artworks/456\"',
        "Find a saved source profile or work URL.",
    ),
}

SORT_TARGETS: dict[str, frozenset[SearchTarget]] = {
    "relevance": frozenset(SCOPE_TARGETS["global"] + ("tasks", "scheduler")),
    "posted-desc": frozenset({"works"}),
    "posted-asc": frozenset({"works"}),
    "created-desc": frozenset({"works", "creators", "tags", "repositories", "subscriptions", "tasks"}),
    "created-asc": frozenset({"works", "creators", "tags", "repositories", "subscriptions", "tasks"}),
    "updated-desc": frozenset({"works", "creators", "tags", "repositories", "subscriptions", "tasks"}),
    "updated-asc": frozenset({"works", "creators", "tags", "repositories", "subscriptions", "tasks"}),
    "name-asc": frozenset({"creators", "tags", "repositories", "subscriptions"}),
    "name-desc": frozenset({"creators", "tags", "repositories", "subscriptions"}),
    "usage-desc": frozenset({"tags"}),
    "last-sync-desc": frozenset({"repositories", "subscriptions"}),
    "last-sync-asc": frozenset({"repositories", "subscriptions"}),
    "title-asc": frozenset({"works"}),
    "title-desc": frozenset({"works"}),
}

TASK_STATUSES = frozenset({
    "enqueued",
    "pending",
    "running",
    "downloading",
    "downloaded",
    "importing",
    "complete",
    "failed",
    "stale",
    "paused",
    "cancelled",
})

TASK_KINDS = frozenset({"download", "import", "admin"})

CONFLICTING_IS_VALUES = (
    frozenset({"nsfw", "sfw"}),
    frozenset({"ai", "human"}),
    frozenset({"visible", "trashed"}),
    frozenset({"active", "inactive"}),
    frozenset({"enabled", "disabled"}),
    frozenset({"auth-ok", "auth-error"}),
    frozenset({"sync-enabled", "sync-disabled"}),
)

DATE_RE = re.compile(r"^(?P<operator><=|>=|<|>|=)?(?P<value>\d{4}-\d{2}-\d{2}(?:[Tt][^\s]+)?)$")
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")


@dataclass(frozen=True)
class SearchDiagnostic:
    code: str
    message: str
    start: int
    end: int
    token: str
    suggestions: tuple[str, ...] = ()

    def payload(self) -> dict:
        data = asdict(self)
        data["suggestions"] = list(self.suggestions)
        return data


class SearchQueryError(ValueError):
    """Raised when a query cannot be executed without guessing."""

    def __init__(self, diagnostic: SearchDiagnostic):
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


@dataclass(frozen=True)
class SearchTerm:
    value: str
    quoted: bool
    start: int
    end: int

    def payload(self) -> dict:
        return {
            "kind": "text",
            "value": self.value,
            "quoted": self.quoted,
            "start": self.start,
            "end": self.end,
        }


@dataclass(frozen=True)
class SearchQualifier:
    key: str
    value: str
    negated: bool
    quoted: bool
    start: int
    end: int

    def payload(self) -> dict:
        return {
            "kind": "qualifier",
            "key": self.key,
            "value": self.value,
            "negated": self.negated,
            "quoted": self.quoted,
            "start": self.start,
            "end": self.end,
        }


SearchToken = SearchTerm | SearchQualifier


@dataclass(frozen=True)
class SearchQuery:
    raw: str
    canonical: str
    scope: SearchScope
    tokens: tuple[SearchToken, ...]
    targets: tuple[SearchTarget, ...]

    @property
    def terms(self) -> tuple[SearchTerm, ...]:
        return tuple(token for token in self.tokens if isinstance(token, SearchTerm))

    @property
    def qualifiers(self) -> tuple[SearchQualifier, ...]:
        return tuple(token for token in self.tokens if isinstance(token, SearchQualifier))

    def values(self, key: str, *, negated: bool | None = None) -> tuple[str, ...]:
        return tuple(
            token.value
            for token in self.qualifiers
            if token.key == key and (negated is None or token.negated is negated)
        )

    def payload(self) -> dict:
        return {
            "raw": self.raw,
            "canonical": self.canonical,
            "scope": self.scope,
            "targets": list(self.targets),
            "tokens": [token.payload() for token in self.tokens],
        }


@dataclass(frozen=True)
class _Lexeme:
    raw: str
    start: int
    end: int


def _error(code: str, message: str, lexeme: _Lexeme, suggestions: Iterable[str] = ()) -> SearchQueryError:
    return SearchQueryError(SearchDiagnostic(
        code=code,
        message=message,
        start=lexeme.start,
        end=lexeme.end,
        token=lexeme.raw,
        suggestions=tuple(suggestions),
    ))


def _lex(query: str) -> list[_Lexeme]:
    lexemes: list[_Lexeme] = []
    index = 0
    length = len(query)
    while index < length:
        while index < length and query[index].isspace():
            index += 1
        if index >= length:
            break
        start = index
        quoted = False
        escaped = False
        while index < length:
            char = query[index]
            if escaped:
                escaped = False
            elif char == "\\" and quoted:
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif char.isspace() and not quoted:
                break
            index += 1
        raw = query[start:index]
        if quoted:
            raise _error("unclosed_quote", "Quoted search value is not closed.", _Lexeme(raw, start, index))
        lexemes.append(_Lexeme(raw=raw, start=start, end=index))
    return lexemes


def _find_colon(value: str) -> int:
    quoted = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char in {":", "："} and not quoted:
            return index
    return -1


def _decode_value(raw: str, lexeme: _Lexeme) -> tuple[str, bool]:
    if not raw:
        raise _error("missing_value", "Search qualifier requires a value.", lexeme)
    if not raw.startswith('"'):
        if '"' in raw:
            raise _error("invalid_quote", "Quotes must wrap the complete qualifier value.", lexeme)
        return raw, False
    if len(raw) < 2 or not raw.endswith('"'):
        raise _error("unclosed_quote", "Quoted search value is not closed.", lexeme)
    decoded: list[str] = []
    escaped = False
    for char in raw[1:-1]:
        if escaped:
            decoded.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            decoded.append(char)
    if escaped:
        decoded.append("\\")
    value = "".join(decoded)
    if not value:
        raise _error("missing_value", "Search qualifier requires a value.", lexeme)
    return value, True


def _canonical_value(value: str) -> str:
    if not value or any(char.isspace() for char in value) or any(char in {'"', "\\"} for char in value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _canonical_token(token: SearchToken) -> str:
    if isinstance(token, SearchTerm):
        return _canonical_value(token.value) if token.quoted or any(char.isspace() for char in token.value) else token.value
    prefix = "-" if token.negated else ""
    value = _canonical_value(token.value)
    if token.key == "url" and not value.startswith('"'):
        value = f'"{token.value}"'
    return f"{prefix}{token.key}:{value}"


def _parse_date(value: str, lexeme: _Lexeme) -> None:
    match = DATE_RE.match(value)
    if not match:
        raise _error(
            "invalid_date",
            "Date qualifiers use an ISO date with an optional comparison operator.",
            lexeme,
            (">=2026-01-01", "<2026-08-01"),
        )
    date_value = match.group("value")
    try:
        datetime.fromisoformat(date_value.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise _error("invalid_date", "Search date is not a valid ISO date.", lexeme) from exc


def _validate_value(token: SearchQualifier, lexeme: _Lexeme) -> SearchQualifier:
    value = token.value.strip()
    if token.key == "type":
        value = TYPE_ALIASES.get(value.lower(), value.lower())
        if value not in TYPE_TARGETS:
            raise _error("invalid_value", f"Unknown search type: {token.value}", lexeme, TYPE_TARGETS)
    elif token.key == "is":
        value = value.lower()
        if value not in IS_TARGETS:
            raise _error("invalid_value", f"Unknown is: value: {token.value}", lexeme, IS_TARGETS)
    elif token.key == "has":
        value = value.lower()
        if value not in HAS_TARGETS:
            raise _error("invalid_value", f"Unknown has: value: {token.value}", lexeme, HAS_TARGETS)
    elif token.key == "sort":
        value = value.lower()
        if value not in SORT_TARGETS:
            raise _error("invalid_value", f"Unknown sort order: {token.value}", lexeme, SORT_TARGETS)
    elif token.key == "status":
        value = value.lower()
        if value not in TASK_STATUSES:
            raise _error("invalid_value", f"Unknown task status: {token.value}", lexeme, TASK_STATUSES)
    elif token.key == "kind":
        value = value.lower()
        if value not in TASK_KINDS:
            raise _error("invalid_value", f"Unknown task kind: {token.value}", lexeme, TASK_KINDS)
    elif token.key in {"posted", "created", "updated", "synced"}:
        _parse_date(value, lexeme)
    elif token.key == "source":
        value = "x" if value.lower() == "twitter" else value.lower()
    elif token.key in {"uid", "pid"}:
        try:
            source, identity = parse_source_identity(value)
        except ValueError as exc:
            raise _error(
                "invalid_identity",
                str(exc),
                lexeme,
                tuple(f"{source}/123" for source in sorted(IDENTITY_SOURCES) if source != "manual")[:5],
            ) from exc
        value = f"{source}/{identity}"
    elif token.key == "url":
        parsed_url = parse_source_url(value)
        if parsed_url is None:
            raise _error(
                "unsupported_url",
                "URL is not a supported creator or work URL.",
                lexeme,
            )
        value = parsed_url.normalized_url

    return SearchQualifier(
        key=token.key,
        value=value,
        negated=token.negated,
        quoted=token.quoted,
        start=token.start,
        end=token.end,
    )


def _targets_for_token(token: SearchQualifier) -> frozenset[SearchTarget] | None:
    if token.key == "type":
        return frozenset({TYPE_TARGETS[token.value]})
    if token.key == "is":
        return IS_TARGETS[token.value]
    if token.key == "has":
        return HAS_TARGETS[token.value]
    if token.key == "sort":
        return SORT_TARGETS[token.value]
    if token.key == "url":
        parsed_url = parse_source_url(token.value)
        if parsed_url and parsed_url.kind == "work":
            return frozenset({"works"})
    return QUALIFIER_TARGETS.get(token.key)


def _validate_query(tokens: tuple[SearchToken, ...], scope: SearchScope) -> tuple[SearchTarget, ...]:
    allowed = set(SCOPE_TARGETS[scope])
    qualifiers = tuple(token for token in tokens if isinstance(token, SearchQualifier))

    type_tokens = [token for token in qualifiers if token.key == "type"]
    if type_tokens:
        requested = {TYPE_TARGETS[token.value] for token in type_tokens}
        unavailable = requested - allowed
        if unavailable:
            token = type_tokens[0]
            raise SearchQueryError(SearchDiagnostic(
                code="type_not_available",
                message=f"Requested type is not available in the {scope} search scope.",
                start=token.start,
                end=token.end,
                token=_canonical_token(token),
                suggestions=tuple(f"type:{key}" for key, target in TYPE_TARGETS.items() if target in allowed),
            ))
        allowed &= requested

    for token in qualifiers:
        if token.key == "type":
            continue
        token_targets = _targets_for_token(token)
        if token_targets is None:
            continue
        compatible = allowed & set(token_targets)
        if not compatible:
            raise SearchQueryError(SearchDiagnostic(
                code="qualifier_not_available",
                message=f"{token.key}: is not available for the selected search type.",
                start=token.start,
                end=token.end,
                token=_canonical_token(token),
            ))
        # In global mode a domain-specific qualifier intentionally narrows the
        # result groups it can describe. Scoped list pages are already singular.
        if scope == "global" and not type_tokens:
            allowed = compatible

    positive_is = {token.value for token in qualifiers if token.key == "is" and not token.negated}
    for conflict in CONFLICTING_IS_VALUES:
        found = positive_is & conflict
        if len(found) > 1:
            token = next(token for token in qualifiers if token.key == "is" and token.value in found)
            raise SearchQueryError(SearchDiagnostic(
                code="conflicting_values",
                message=f"Mutually exclusive search states cannot be combined: {', '.join(sorted(found))}.",
                start=token.start,
                end=token.end,
                token=_canonical_token(token),
            ))

    seen_positive = {(token.key, token.value) for token in qualifiers if not token.negated}
    seen_negative = {(token.key, token.value) for token in qualifiers if token.negated}
    overlap = seen_positive & seen_negative
    if overlap:
        key, value = next(iter(overlap))
        token = next(token for token in qualifiers if token.key == key and token.value == value)
        raise SearchQueryError(SearchDiagnostic(
            code="conflicting_values",
            message=f"The query both includes and excludes {key}:{value}.",
            start=token.start,
            end=token.end,
            token=_canonical_token(token),
        ))

    sort_tokens = [token for token in qualifiers if token.key == "sort"]
    if len(sort_tokens) > 1:
        token = sort_tokens[1]
        raise SearchQueryError(SearchDiagnostic(
            code="duplicate_sort",
            message="A search query can use only one sort: qualifier.",
            start=token.start,
            end=token.end,
            token=_canonical_token(token),
        ))

    if not allowed:
        token = qualifiers[0] if qualifiers else SearchTerm("", False, 0, 0)
        raise SearchQueryError(SearchDiagnostic(
            code="no_compatible_type",
            message="No result type supports this combination of qualifiers.",
            start=token.start,
            end=token.end,
            token=_canonical_token(token),
        ))
    return tuple(target for target in SCOPE_TARGETS[scope] if target in allowed)


def parse_search_query(query: str, scope: SearchScope = "global") -> SearchQuery:
    """Parse, validate and canonicalize a compound search query."""

    if scope not in SCOPE_TARGETS:
        raise ValueError(f"Unknown search scope: {scope}")

    tokens: list[SearchToken] = []
    for lexeme in _lex(query):
        raw_url = lexeme.raw[1:] if lexeme.raw.startswith("-") else lexeme.raw
        parsed_url = parse_source_url(raw_url)
        if raw_url.lower().startswith(("http://", "https://")):
            if parsed_url is None:
                raise _error(
                    "unsupported_url",
                    "URL is not a supported creator or work URL.",
                    lexeme,
                )
            token = SearchQualifier(
                key="url",
                value=parsed_url.normalized_url,
                negated=lexeme.raw.startswith("-"),
                quoted=True,
                start=lexeme.start,
                end=lexeme.end,
            )
            tokens.append(token)
            continue
        colon_at = _find_colon(lexeme.raw)
        negated = lexeme.raw.startswith("-")
        key_start = 1 if negated else 0
        if colon_at > key_start:
            key = lexeme.raw[key_start:colon_at].lower()
            if KEY_RE.match(key):
                if key not in QUALIFIER_KEYS:
                    raise _error("unknown_qualifier", f"Unknown search qualifier: {key}", lexeme, QUALIFIER_KEYS)
                if negated and key not in NEGATABLE_KEYS:
                    raise _error("invalid_negation", f"{key}: cannot be negated.", lexeme)
                value, quoted = _decode_value(lexeme.raw[colon_at + 1:], lexeme)
                token = SearchQualifier(
                    key=key,
                    value=value,
                    negated=negated,
                    quoted=quoted,
                    start=lexeme.start,
                    end=lexeme.end,
                )
                tokens.append(_validate_value(token, lexeme))
                continue
        value, quoted = _decode_value(lexeme.raw, lexeme)
        if not quoted and value.upper() == "OR":
            raise _error(
                "unsupported_operator",
                "Explicit OR is not supported; repeat one qualifier for OR semantics.",
                lexeme,
            )
        if not quoted and any(char in value for char in "()"):
            raise _error(
                "unsupported_grouping",
                "Parenthesized search expressions are not supported.",
                lexeme,
            )
        tokens.append(SearchTerm(value=value, quoted=quoted, start=lexeme.start, end=lexeme.end))

    token_tuple = tuple(tokens)
    targets = _validate_query(token_tuple, scope)
    canonical = " ".join(_canonical_token(token) for token in token_tuple)
    return SearchQuery(raw=query, canonical=canonical, scope=scope, tokens=token_tuple, targets=targets)


def compose_search_query(
    query: str,
    scope: SearchScope,
    *,
    key: str,
    value: str | None,
    operation: Literal["set", "add", "toggle", "remove", "replace-group"] = "set",
    negated: bool = False,
    replace_values: Iterable[str] = (),
) -> SearchQuery:
    """Apply one visual-filter edit and return the canonical parsed query."""

    parsed = parse_search_query(query, scope)
    normalized_key = key.lower()
    if normalized_key not in QUALIFIER_KEYS:
        raise ValueError(f"Unknown search qualifier: {key}")

    existing = list(parsed.tokens)
    probe = None
    if value is not None:
        raw_probe = f"{'-' if negated else ''}{normalized_key}:{_canonical_value(value)}"
        probe_query = parse_search_query(raw_probe, scope)
        probe = probe_query.qualifiers[0]

    if operation == "set":
        existing = [
            token for token in existing
            if not (isinstance(token, SearchQualifier) and token.key == normalized_key)
        ]
        if probe:
            existing.append(probe)
    elif operation == "add":
        if probe and not any(
            isinstance(token, SearchQualifier)
            and token.key == probe.key
            and token.value == probe.value
            and token.negated == probe.negated
            for token in existing
        ):
            existing.append(probe)
    elif operation == "toggle":
        if not probe:
            raise ValueError("toggle requires a value")
        match = next((
            index for index, token in enumerate(existing)
            if isinstance(token, SearchQualifier)
            and token.key == probe.key
            and token.value == probe.value
            and token.negated == probe.negated
        ), None)
        if match is None:
            existing.append(probe)
        else:
            existing.pop(match)
    elif operation == "remove":
        existing = [
            token for token in existing
            if not (
                isinstance(token, SearchQualifier)
                and token.key == normalized_key
                and (value is None or token.value == (probe.value if probe else value))
                and (value is None or token.negated == negated)
            )
        ]
    elif operation == "replace-group":
        normalized_values = set()
        for item in replace_values:
            probe_item = parse_search_query(f"{normalized_key}:{_canonical_value(item)}", scope).qualifiers[0]
            normalized_values.add(probe_item.value)
        existing = [
            token for token in existing
            if not (
                isinstance(token, SearchQualifier)
                and token.key == normalized_key
                and token.value in normalized_values
            )
        ]
        if probe:
            existing.append(probe)
    else:
        raise ValueError(f"Unknown compose operation: {operation}")

    return parse_search_query(" ".join(_canonical_token(token) for token in existing), scope)


def qualifier_catalog(scope: SearchScope) -> list[dict]:
    """Return the discoverable language surface for a search input."""

    targets = set(SCOPE_TARGETS[scope])
    catalog = []
    for key in sorted(QUALIFIER_KEYS):
        if key == "type" and scope != "global":
            continue
        values: list[str] = []
        if key == "type":
            values = [name for name, target in TYPE_TARGETS.items() if target in targets]
        elif key == "is":
            values = [value for value, supported in IS_TARGETS.items() if targets & set(supported)]
        elif key == "has":
            values = [value for value, supported in HAS_TARGETS.items() if targets & set(supported)]
        elif key == "sort":
            values = [value for value, supported in SORT_TARGETS.items() if targets & set(supported)]
        elif key == "status" and "tasks" in targets:
            values = sorted(TASK_STATUSES)
        elif key == "kind" and "tasks" in targets:
            values = sorted(TASK_KINDS)
        elif key in {"uid", "pid"}:
            values = [f"{source}/" for source in sorted(IDENTITY_SOURCES)]
        else:
            supported = QUALIFIER_TARGETS.get(key)
            if supported is not None and not (targets & set(supported)):
                continue
        catalog.append({
            "key": key,
            "negatable": key in NEGATABLE_KEYS,
            "values": values,
            "help_id": QUALIFIER_HELP[key][0],
            "example": QUALIFIER_HELP[key][1],
            "description": QUALIFIER_HELP[key][2],
        })
    return catalog
