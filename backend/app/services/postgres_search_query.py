"""PostgreSQL full-text matching, validated filters and deterministic pagination."""
from __future__ import annotations
import json
import re
import unicodedata
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterable
from sqlalchemy import text as sql_text
from app.services.search_index_definitions import INDEX_SETTINGS

from app.services.search_backend import SearchBackendUnavailable

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_FIELD_RE = re.compile(r"^[A-Za-z0-9_]+$")


def normalize_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def tokenize(value: Any) -> list[str]:
    value = normalize_text(value).replace("_", " ")
    result = []
    for part in _WORD_RE.findall(value):
        if any(("\u3400" <= char <= "\u4dbf") or ("\u4e00" <= char <= "\u9fff") or ("\u3040" <= char <= "\u30ff") for char in part):
            result.append(part)
        else:
            result.append(part)
    return result


@dataclass(frozen=True)
class FilterNode:
    kind: str
    value: Any = None
    left: "FilterNode | None" = None
    right: "FilterNode | None" = None


class FilterSyntaxError(ValueError):
    pass


def _filter_tokens(value: str):
    tokens = []
    index = 0
    while index < len(value):
        if value[index].isspace(): index += 1; continue
        if value[index] in "()[],": tokens.append((value[index], value[index])); index += 1; continue
        if value[index:index + 2] in {"!=", ">=", "<="}:
            tokens.append(("OP", value[index:index + 2])); index += 2; continue
        if value[index] in "=><": tokens.append(("OP", value[index])); index += 1; continue
        if value[index] == '"':
            start = index; index += 1; escaped = False
            while index < len(value):
                char = value[index]
                if char == '"' and not escaped: index += 1; break
                escaped = char == "\\" and not escaped
                if char != "\\": escaped = False
                index += 1
            else: raise FilterSyntaxError("unterminated quoted filter value")
            try: tokens.append(("VALUE", json.loads(value[start:index])))
            except json.JSONDecodeError as exc: raise FilterSyntaxError("invalid quoted value") from exc
            continue
        start = index
        while index < len(value) and not value[index].isspace() and value[index] not in "()[],=<>!": index += 1
        raw = value[start:index]
        if not raw: raise FilterSyntaxError("invalid filter token")
        upper = raw.upper()
        if upper in {"AND", "OR", "NOT", "IN"}: tokens.append((upper, upper))
        elif raw == "true": tokens.append(("VALUE", True))
        elif raw == "false": tokens.append(("VALUE", False))
        elif raw == "null": tokens.append(("VALUE", None))
        else:
            try: tokens.append(("VALUE", float(raw) if "." in raw else int(raw)))
            except ValueError: tokens.append(("WORD", raw))
    tokens.append(("EOF", None)); return tokens


class _FilterParser:
    def __init__(self, source: str): self.tokens, self.pos = _filter_tokens(source), 0
    def peek(self): return self.tokens[self.pos]
    def pop(self, expected=None):
        token = self.peek()
        if expected and token[0] != expected: raise FilterSyntaxError(f"expected {expected}")
        self.pos += 1; return token
    def parse(self):
        node = self._or()
        if self.peek()[0] != "EOF": raise FilterSyntaxError("unexpected filter token")
        return node
    def _or(self):
        node = self._and()
        while self.peek()[0] == "OR": self.pop(); node = FilterNode("or", left=node, right=self._and())
        return node
    def _and(self):
        node = self._not()
        while self.peek()[0] == "AND": self.pop(); node = FilterNode("and", left=node, right=self._not())
        return node
    def _not(self):
        if self.peek()[0] == "NOT": self.pop(); return FilterNode("not", left=self._not())
        return self._atom()
    def _atom(self):
        if self.peek()[0] == "(": self.pop(); node = self._or(); self.pop(")"); return node
        kind, field = self.pop()
        if kind not in {"WORD", "VALUE"} or not isinstance(field, str) or not _FIELD_RE.fullmatch(field): raise FilterSyntaxError("invalid filter field")
        kind, operator = self.pop()
        if kind == "NOT":
            self.pop("IN"); operator = "NOT IN"
        elif kind == "IN": operator = "IN"
        elif kind != "OP": raise FilterSyntaxError("missing filter operator")
        if self.peek()[0] == "[":
            self.pop(); values = []
            while self.peek()[0] != "]":
                value_kind, value = self.pop()
                if value_kind not in {"WORD", "VALUE"}: raise FilterSyntaxError("invalid list value")
                values.append(value)
                if self.peek()[0] == ",": self.pop()
                elif self.peek()[0] != "]": raise FilterSyntaxError("missing list separator")
            self.pop("]"); value = values
        else:
            value_kind, value = self.pop()
            if value_kind not in {"WORD", "VALUE"}: raise FilterSyntaxError("missing filter value")
        return FilterNode("cmp", (field, operator, value))

def parse_filter(source: str | list | None):
    if source is None or source == "" or source == []:
        return None
    if isinstance(source, str):
        return _FilterParser(source).parse()
    if not isinstance(source, list):
        raise FilterSyntaxError("filter must be a string or an array")
    def combine(nodes, kind):
        result = None
        for node in nodes:
            if node is not None:
                result = node if result is None else FilterNode(kind, left=result, right=node)
        return result
    groups = []
    for item in source:
        if isinstance(item, str):
            groups.append(parse_filter(item))
        elif isinstance(item, list) and all(isinstance(value, str) for value in item):
            groups.append(combine([parse_filter(value) for value in item], "or"))
        else:
            raise FilterSyntaxError("filter arrays allow strings or one nested array of strings")
    return combine(groups, "and")


def _settings(index_uid: str):
    if index_uid in INDEX_SETTINGS:
        return INDEX_SETTINGS[index_uid]
    return next((value for key, value in INDEX_SETTINGS.items() if str(index_uid).endswith(str(key))), None)


def _allowed(index_uid: str):
    settings = _settings(index_uid)
    if settings is None:
        raise SearchBackendUnavailable(f"search index is unavailable: {index_uid}")
    fields = set(settings.get("filterableAttributes") or ()) | set(settings.get("sortableAttributes") or ())
    if not fields:
        fields |= {"id", "is_nsfw", "visibility", "created_ts", "updated_ts", "title", "name_sort", "heat_score"}
    return fields | {"user_id", "subscription_id"}


def _filter_sql(node, params, allowed, counter):
    if node.kind in {"and", "or"}:
        joiner = " AND " if node.kind == "and" else " OR "
        return f"({_filter_sql(node.left, params, allowed, counter)}{joiner}{_filter_sql(node.right, params, allowed, counter)})"
    if node.kind == "not": return f"(NOT {_filter_sql(node.left, params, allowed, counter)})"
    field, operator, value = node.value
    if field not in allowed: raise FilterSyntaxError(f"filter field is not indexed: {field}")
    path = f"(d.document -> '{field}')"
    values = value if isinstance(value, list) else [value]
    if not values:
        return "TRUE" if operator in {"!=", "NOT IN"} else "FALSE"
    clauses = []
    for item in values:
        key = f"filter_{counter[0]}"; counter[0] += 1
        if item is None:
            clauses.append(f"({path} IS NOT NULL AND {path} <> 'null'::jsonb)") if operator in {"!=", "NOT IN"} else clauses.append(f"({path} IS NULL OR {path} = 'null'::jsonb)")
            continue
        params[key] = json.dumps(item) if isinstance(item, (bool, int, float)) else str(item)
        if isinstance(item, (bool, int, float)):
            clauses.append(f"({path} = CAST(:{key} AS jsonb))")
        else:
            clauses.append(f"(({path} ? :{key}) OR (d.document ->> '{field}') = :{key})")
    if operator in {"=", "IN"}: return "(" + " OR ".join(clauses) + ")"
    if operator in {"!=", "NOT IN"}: return "(NOT (" + " OR ".join(clauses) + "))"
    key = f"filter_{counter[0]}"; counter[0] += 1; params[key] = str(value)
    return f"((d.document ->> '{field}')::numeric {operator} CAST(:{key} AS numeric))"

def _value_equal(actual, wanted):
    if isinstance(actual, list):
        return wanted in actual
    return actual == wanted


def evaluate_filter(node, document):
    if node is None:
        return True
    if node.kind == "and":
        return evaluate_filter(node.left, document) and evaluate_filter(node.right, document)
    if node.kind == "or":
        return evaluate_filter(node.left, document) or evaluate_filter(node.right, document)
    if node.kind == "not":
        return not evaluate_filter(node.left, document)
    field, operator, wanted = node.value
    actual = document.get(field)
    values = wanted if isinstance(wanted, list) else [wanted]
    if operator in {"=", "IN"}:
        return any(_value_equal(actual, value) for value in values)
    if operator in {"!=", "NOT IN"}:
        return all(not _value_equal(actual, value) for value in values)
    if actual is None:
        return False
    try:
        return {"<": actual < wanted, "<=": actual <= wanted, ">": actual > wanted, ">=": actual >= wanted}[operator]
    except (KeyError, TypeError):
        return False

def _damerau_one(a: str, b: str) -> bool:
    if a == b: return True
    if abs(len(a) - len(b)) > 1: return False
    if len(a) == len(b):
        differences = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
        if len(differences) == 1:
            return True
        return len(differences) == 2 and differences[1] == differences[0] + 1 and a[differences[0]] == b[differences[1]] and a[differences[1]] == b[differences[0]]
    if len(a) > len(b): a, b = b, a
    i = j = changes = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1; j += 1
        else:
            changes += 1; j += 1
            if changes > 1: return False
    return True


def _token_match(query: str, stored: str):
    if query == stored: return True, 0
    if stored.startswith(query): return True, 1
    if query.isdecimal() or len(query) < 5: return False, 99
    if _damerau_one(query, stored): return True, 2
    if len(query) >= 9 and len(query) == len(stored) and sum(a != b for a, b in zip(query, stored)) <= 2:
        return True, 3
    return False, 99


def score_text(value: str, query: str, *, matching_strategy="all"):
    query_tokens, stored_tokens = tokenize(query), tokenize(value)
    if not query_tokens:
        return True, (0, 0, 0)
    def score(terms):
        costs = []
        for term in terms:
            matches = [cost for stored in stored_tokens for hit, cost in [_token_match(term, stored)] if hit]
            if not matches: return False, (99, 99, 99)
            costs.append(min(matches))
        return True, (sum(costs), max(costs), -len(terms))
    matched, rank = score(query_tokens)
    if matched or matching_strategy != "last":
        return matched, rank
    for size in range(len(query_tokens) - 1, 0, -1):
        matched, rank = score(query_tokens[:size])
        if matched:
            return True, (rank[0] + len(query_tokens) - size, rank[1], -size)
    return False, (99, 99, 99)


def _target(index_uid: str):
    for suffix in ("works", "creators", "tags", "repositories", "subscriptions"):
        if str(index_uid).endswith(suffix):
            return suffix
    return "works"


def _sort_sql(sort, allowed):
    values = [sort] if isinstance(sort, str) else list(sort or [])
    clauses = []
    for item in values:
        field, sep, direction = str(item).partition(":")
        if not sep or field not in allowed or direction not in {"asc", "desc"}:
            raise ValueError(f"invalid search sort: {item}")
        clauses.append(f"(d.document ->> '{field}') {direction} NULLS LAST")
    return " ORDER BY " + ", ".join(clauses) if clauses else ""


def _retrieve(document, attributes):
    if not attributes or "*" in attributes:
        return dict(document)
    keep = set(attributes) | {"id"}
    return {key: value for key, value in document.items() if key in keep}


_NUMERIC_SORT_FIELDS = {
    "posted_ts", "created_ts", "updated_ts", "synced_ts", "heat_score",
    "usage_count", "asset_count",
}
from app.services.search_pagination import MEILI_SHUFFLE_FIELDS
_NUMERIC_SORT_FIELDS.update(MEILI_SHUFFLE_FIELDS)

def _typed_sort_sql(sort, allowed):
    values = [sort] if isinstance(sort, str) else list(sort or [])
    clauses = []
    for item in values:
        field, sep, direction = str(item).partition(":")
        if not sep or field not in allowed or direction not in {"asc", "desc"}:
            raise ValueError(f"invalid search sort: {item}")
        if field in _NUMERIC_SORT_FIELDS:
            expression = f"NULLIF(d.document ->> '{field}', '')::numeric"
        else:
            expression = f"d.document ->> '{field}'"
        clauses.append(f"{expression} {direction} NULLS LAST")
    if clauses and not any(str(item).partition(":")[0] == "id" for item in values):
        clauses.append("d.entity_id ASC")
    return " ORDER BY " + ", ".join(clauses) if clauses else ""


def _swap_variants(term: str) -> list[str]:
    return [term[:i] + term[i + 1] + term[i] + term[i + 2:] for i in range(len(term) - 1) if term[i] != term[i + 1]]


def _term_sql(term: str, params: dict[str, Any], counter: list[int], *, prefix: bool):
    key = f"term_{counter[0]}"; counter[0] += 1; params[key] = term
    if any(("\u3400" <= char <= "\u9fff") or ("\u3040" <= char <= "\u30ff") for char in term):
        return f"position(:{key} in d.search_text) > 0"
    exact_key = f"exact_{counter[0]}"; counter[0] += 1; params[exact_key] = [term]
    clauses = [f"tok = ANY(CAST(:{exact_key} AS text[]))"]
    if prefix and not term.isdecimal():
        prefix_key = f"prefix_{counter[0]}"; counter[0] += 1; params[prefix_key] = [term + "%"]
        clauses.append(f"tok LIKE ANY(CAST(:{prefix_key} AS text[]))")
    distance = 0 if term.isdecimal() or len(term) > 255 else 2 if len(term) >= 9 else 1 if len(term) >= 5 else 0
    if distance:
        swap_values = _swap_variants(term)
        if swap_values:
            swap_key = f"swap_{counter[0]}"; counter[0] += 1; params[swap_key] = swap_values
            clauses.append(f"tok = ANY(CAST(:{swap_key} AS text[]))")
        clauses.append(f"CASE WHEN char_length(tok) <= 255 AND char_length(:{key}) <= 255 THEN levenshtein_less_equal(tok, :{key}, {distance}) <= {distance} ELSE FALSE END")
    return "EXISTS (SELECT 1 FROM unnest(d.tokens) AS tok WHERE " + " OR ".join(clauses) + ")"


def _text_clause(query: str, strategy: str, params: dict[str, Any], counter: list[int], terms_override=None):
    terms = list(terms_override or tokenize(query))
    if not terms: return "", terms
    clauses = [_term_sql(term, params, counter, prefix=(index == len(terms) - 1)) for index, term in enumerate(terms)]
    return "(" + " AND ".join(clauses) + ")", terms


def _text_rank_sql(query: str, params: dict[str, Any], counter: list[int], terms_override=None):
    terms = list(terms_override or tokenize(query))
    if not terms: return "0"
    ranks = []
    for index, term in enumerate(terms):
        key = f"rank_{counter[0]}"; counter[0] += 1; params[key] = term
        exact = f"EXISTS (SELECT 1 FROM unnest(d.tokens) AS tok WHERE tok = :{key})"
        prefix = f"EXISTS (SELECT 1 FROM unnest(d.tokens) AS tok WHERE tok LIKE :{key} || '%')"
        ranks.append(f"CASE WHEN {exact} THEN 0 WHEN {prefix} THEN 1 ELSE 2 END")
    return "(" + " + ".join(ranks) + ")"

async def _query_once(db, index_uid, text_value, kwargs, terms_override=None):
    params={"index_uid":index_uid}
    where=[]
    node=parse_filter(kwargs.get("filter"))
    if node:where.append(_filter_sql(node,params,_allowed(index_uid),[0]))
    query_text=str(text_value or "")
    strategy=str(kwargs.get("matching_strategy") or ("last" if _target(index_uid)=="works" else "all"))
    if query_text:
        clause,_=_text_clause(query_text,strategy,params,[1000],terms_override=terms_override)
        if clause:where.append(clause)
    order=_typed_sort_sql(kwargs.get("sort"),set(_settings(index_uid).get("sortableAttributes") or [])) or " ORDER BY d.entity_id ASC"
    if query_text and not kwargs.get("sort"):
        rank=_text_rank_sql(query_text,params,[2000],terms_override=terms_override)
        order=" ORDER BY "+rank+", d.entity_id ASC"
    params["page_limit"]=max(0,int(20 if kwargs.get("limit") is None else kwargs["limit"]))
    params["page_offset"]=max(0,int(kwargs.get("offset") or 0))
    predicate=" WHERE "+" AND ".join(where) if where else ""
    statement="""WITH source AS MATERIALIZED (
        SELECT storage_id FROM search_pg_indexes WHERE uid=:index_uid
    ), matched AS MATERIALIZED (
        SELECT d.entity_id,d.document,d.tokens FROM source i
        JOIN search_pg_documents d ON d.storage_id=i.storage_id"""+predicate+"""
    ), total AS (SELECT count(*) AS total_hits FROM matched), page AS (
        SELECT d.entity_id,d.document,row_number() OVER ("""+order+""") AS position
        FROM matched d"""+order+""" LIMIT :page_limit OFFSET :page_offset
    ) SELECT total.total_hits,EXISTS(SELECT 1 FROM source) AS index_exists,
        page.entity_id,page.document FROM total LEFT JOIN page ON TRUE ORDER BY page.position"""
    rows=(await db.execute(sql_text(statement),params)).mappings().all()
    if not rows[0]["index_exists"]:
        raise SearchBackendUnavailable(f"search index is unavailable: {index_uid}")
    total=int(rows[0]["total_hits"])
    hits=[_retrieve(dict(row["document"] or {}),kwargs.get("attributes_to_retrieve"))for row in rows if row["entity_id"] is not None]
    return SimpleNamespace(hits=hits,estimated_total_hits=total,total_hits=total)


def _match_rank(document, stored, query, strategy):
    matched, rank = score_text(stored, query, matching_strategy=strategy)
    return (*rank, str(document.get("id", ""))) if matched else (999, 999, 999, str(document.get("id", "")))


async def search_one(db, index_uid: str, text: str, kwargs: dict[str, Any]):
    if _settings(index_uid) is None:
        raise SearchBackendUnavailable(f"search index is unavailable: {index_uid}")
    strategy = str(kwargs.get("matching_strategy") or ("last" if _target(index_uid) == "works" else "all"))
    result = await _query_once(db, index_uid, text, kwargs)
    if strategy == "last" and text and result.total_hits == 0:
        terms = tokenize(text)
        for size in range(len(terms) - 1, 0, -1):
            result = await _query_once(db, index_uid, text, kwargs, terms_override=terms[:size])
            if result.total_hits > 0: break
    return result

async def multi_search(db, prepared: Iterable[tuple[str, str, dict[str, Any]]], text: str):
    return [await search_one(db, index_uid, text, kwargs) for _target_name, index_uid, kwargs in prepared]
