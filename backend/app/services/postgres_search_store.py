"""Transactional PostgreSQL index storage behind the search delivery port.

Schema changes belong to Alembic; requests never create tables or extensions.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx

from sqlalchemy import text

from app.config import settings
from app.database import async_session

INDEX_TABLE = "search_pg_indexes"
DOC_TABLE = "search_pg_documents"
TASK_TABLE = "search_pg_tasks"
_SCHEMA_READY = False
_DEADLINE = contextvars.ContextVar("pg_search_deadline", default=None)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_text(value: Any) -> str:
    """Canonical normalization for stored search tokens."""
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def tokenize(value: Any) -> list[str]:
    """Return word tokens and contiguous one/two-character CJK grams."""
    normalized = normalize_text(value)
    tokens: set[str] = set()
    for word in _WORD_RE.findall(normalized.replace("_", " ")):
        if word:
            tokens.add(word)
    segment: list[str] = []
    def flush() -> None:
        if not segment:
            return
        tokens.update(segment)
        tokens.update("".join(segment[i:i + 2]) for i in range(len(segment) - 1))
        segment.clear()
    for char in normalized:
        if "㐀" <= char <= "鿿" or "぀" <= char <= "ヿ":
            segment.append(char)
        else:
            flush()
    flush()
    return sorted(token for token in tokens if token)




def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _not_found(code: str, detail: str):
    request = httpx.Request("GET", "http://postgresql-search.invalid")
    response = httpx.Response(404, request=request, json={"code": code, "message": detail})
    raise httpx.HTTPStatusError(f"{code}: {detail}", request=request, response=response)


async def _set_sql_budget(db, deadline: float) -> None:
    milliseconds = max(1, int((deadline - time.monotonic()) * 1000))
    await db.execute(text("SELECT set_config('statement_timeout', :value, true)"), {"value": str(milliseconds)})


async def _set_sql_budget_if_available(db) -> None:
    deadline = _DEADLINE.get()
    if deadline is not None:
        await _set_sql_budget(db, deadline)


async def _ensure() -> None:
    """Only verify schema existence; Alembic owns DDL."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    async with async_session() as db:
        rows = (await db.execute(text("SELECT to_regclass(:index_table), to_regclass(:doc_table), to_regclass(:task_table)"),
                                 {"index_table": INDEX_TABLE, "doc_table": DOC_TABLE, "task_table": TASK_TABLE})).one()
    if not all(rows):
        raise RuntimeError("PG search adapter schema is not initialized; run alembic upgrade head")
    _SCHEMA_READY = True


async def _index(db, uid: str):
    return (await db.execute(text(f"SELECT uid,storage_id,settings FROM {INDEX_TABLE} WHERE uid=:uid FOR UPDATE"), {"uid": uid})).mappings().one_or_none()


async def _task(db, index_uid: str | None, task_type: str, details: dict[str, Any], *, error=None) -> int:
    started = _now()
    row = (await db.execute(text(f"""
        INSERT INTO {TASK_TABLE}(index_uid,type,status,started_at,finished_at,details,error)
        VALUES (:index_uid,:type,'succeeded',:started_at,:finished_at,CAST(:details AS jsonb),CAST(:error AS jsonb))
        RETURNING uid
    """), {"index_uid": index_uid, "type": task_type, "started_at": started,
           "finished_at": started, "details": _json(details), "error": _json(error) if error else None})).scalar_one()
    return int(row)


def _search_fields(settings_json: dict[str, Any]) -> list[str]:
    values = settings_json.get("searchableAttributes", [])
    return [str(value) for value in values if isinstance(value, str)]


def _document_fields(document: dict[str, Any], settings_json: dict[str, Any]) -> list[str]:
    fields = _search_fields(settings_json)
    values: list[str] = []
    for field in fields:
        value = document.get(field)
        if isinstance(value, list):
            values.extend(str(item) for item in value if item is not None)
        elif value is not None:
            values.append(str(value))
    return values


def _doc_values(document: dict[str, Any], settings_json: dict[str, Any]) -> tuple[str, list[str]]:
    fields = _document_fields(document, settings_json)
    search_text = normalize_text(" ".join(fields))
    return search_text, tokenize(search_text)


def _task_response(uid: int, index_uid: str | None, task_type: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"taskUid": uid, "indexUid": index_uid, "status": "succeeded",
            "type": task_type, "details": details, "error": None,
            "enqueuedAt": int(time.time() * 1000), "finishedAt": int(time.time() * 1000)}


async def _create_index(payload: dict[str, Any]) -> dict[str, Any]:
    uid = str(payload.get("uid") or "")
    if not uid:
        raise ValueError("index creation requires uid")
    settings_json = dict(payload.get("settings") or {})
    storage_id = uuid4()
    async with async_session() as db:
        await _set_sql_budget_if_available(db)
        await db.execute(text(f"INSERT INTO {INDEX_TABLE}(uid,storage_id,settings) VALUES (:uid,:storage_id,CAST(:settings AS jsonb)) ON CONFLICT (uid) DO NOTHING"),
                         {"uid": uid, "storage_id": storage_id, "settings": _json(settings_json)})
        task = await _task(db, uid, "indexCreation", {"storage_id": str(storage_id)})
        await db.commit()
    return _task_response(task, uid, "indexCreation", {"storage_id": str(storage_id)})


async def _write_documents_in_session(db, index_uid: str, payload: Any) -> dict[str, Any]:
    documents = payload if isinstance(payload, list) else payload.get("documents", [])
    if not isinstance(documents, list) or len(documents) > 500:
        raise ValueError("document write requires at most 500 documents")
    await _set_sql_budget_if_available(db)
    row = await _index(db, index_uid)
    if row is None:
        _not_found("index_not_found", f"index not found: {index_uid}")
    storage_id = row["storage_id"]; settings_json = dict(row["settings"] or {})
    values = []
    for document in documents:
        if not isinstance(document, dict) or document.get("id") is None:
            raise ValueError("each document requires an id")
        search_text, tokens = _doc_values(document, settings_json)
        values.append({"storage_id": storage_id, "entity_id": str(document["id"]), "document": _json(document),
                       "search_text": search_text, "tokens": tokens})
    if values:
        await db.execute(text(f"""
            INSERT INTO {DOC_TABLE}(storage_id,entity_id,document,search_text,tokens,search_vector)
            VALUES (:storage_id,:entity_id,CAST(:document AS jsonb),:search_text,:tokens,to_tsvector('simple',:search_text))
            ON CONFLICT(storage_id,entity_id) DO UPDATE SET document=EXCLUDED.document,
                search_text=EXCLUDED.search_text,tokens=EXCLUDED.tokens,search_vector=EXCLUDED.search_vector
        """), values)
    task = await _task(db, index_uid, "documentAdditionOrUpdate",
                       {"receivedDocuments": len(documents), "indexedDocuments": len(values), "storage_id": str(storage_id)})
    return _task_response(task, index_uid, "documentAdditionOrUpdate", {"receivedDocuments": len(documents)})


async def _write_documents(index_uid: str, payload: Any) -> dict[str, Any]:
    async with async_session() as db:
        result = await _write_documents_in_session(db, index_uid, payload)
        await db.commit()
        return result

async def _delete_documents_in_session(db, index_uid: str, payload: Any) -> dict[str, Any]:
    values = payload if isinstance(payload, list) else payload.get("ids", [])
    if len(values) > 500:
        raise ValueError("document deletion requires at most 500 ids")
    await _set_sql_budget_if_available(db)
    row = await _index(db, index_uid)
    if row is None:
        _not_found("index_not_found", f"index not found: {index_uid}")
    result = await db.execute(text(f"DELETE FROM {DOC_TABLE} WHERE storage_id=:storage_id AND entity_id = ANY(:ids)"),
                              {"storage_id": row["storage_id"], "ids": [str(value) for value in values]})
    task = await _task(db, index_uid, "documentDeletion",
                       {"deletedDocuments": int(result.rowcount or 0), "requestedDocuments": len(values)})
    return _task_response(task, index_uid, "documentDeletion", {"deletedDocuments": int(result.rowcount or 0)})


async def _delete_documents(index_uid: str, payload: Any) -> dict[str, Any]:
    async with async_session() as db:
        result = await _delete_documents_in_session(db, index_uid, payload)
        await db.commit()
        return result

async def _settings(index_uid: str, method: str, payload: Any = None) -> dict[str, Any]:
    async with async_session() as db:
        await _set_sql_budget_if_available(db)
        row = await _index(db, index_uid)
        if row is None and method == "PATCH":
            row = {"uid": index_uid, "storage_id": uuid4(), "settings": {}}
            await db.execute(text(f"INSERT INTO {INDEX_TABLE}(uid,storage_id,settings) VALUES (:uid,:storage_id,CAST(:settings AS jsonb))"),
                             {"uid": index_uid, "storage_id": row["storage_id"], "settings": "{}"})
        elif row is None:
            _not_found("index_not_found", f"index not found: {index_uid}")
        current = dict(row["settings"] or {})
        if method == "GET":
            return current
        if not isinstance(payload, dict):
            raise ValueError("settings payload must be an object")
        current.update(payload)
        await db.execute(text(f"UPDATE {INDEX_TABLE} SET settings=CAST(:settings AS jsonb) WHERE uid=:uid"),
                         {"settings": _json(current), "uid": index_uid})
        task = await _task(db, index_uid, "settingsUpdate", {"settings": current})
        await db.commit()
    return _task_response(task, index_uid, "settingsUpdate", {"settings": current})


async def _swap(payload: Any) -> dict[str, Any]:
    pairs = (payload or {}).get("indexes", []) if isinstance(payload, dict) else payload
    if not isinstance(pairs, list): raise ValueError("swap payload requires indexes")
    normalized = []
    for item in pairs:
        pair = item.get("indexes") if isinstance(item, dict) else item
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError("swap indexes requires uid pairs")
        normalized.append([str(pair[0]), str(pair[1])])
    pairs = normalized
    async with async_session() as db:
        await _set_sql_budget_if_available(db)
        for pair in pairs:
            left, right = str(pair[0]), str(pair[1])
            rows = (await db.execute(text(f"SELECT uid,storage_id,settings FROM {INDEX_TABLE} WHERE uid IN (:left,:right) FOR UPDATE"),
                                     {"left": left, "right": right})).mappings().all()
            if len(rows) != 2: _not_found("index_not_found", "swap index missing")
            by_uid = {str(row["uid"]): row for row in rows}
            await db.execute(text(f"UPDATE {INDEX_TABLE} SET storage_id=:storage_id, settings=CAST(:settings AS jsonb) WHERE uid=:uid"),
                             {"uid": left, "storage_id": by_uid[right]["storage_id"], "settings": _json(by_uid[right]["settings"] or {})})
            await db.execute(text(f"UPDATE {INDEX_TABLE} SET storage_id=:storage_id, settings=CAST(:settings AS jsonb) WHERE uid=:uid"),
                             {"uid": right, "storage_id": by_uid[left]["storage_id"], "settings": _json(by_uid[left]["settings"] or {})})
        task = await _task(db, None, "indexSwap", {"pairs": pairs})
        await db.commit()
    return _task_response(task, None, "indexSwap", {"pairs": pairs})


async def _drop(index_uid: str) -> dict[str, Any]:
    async with async_session() as db:
        await _set_sql_budget_if_available(db)
        row = await _index(db, index_uid)
        if row is None: _not_found("index_not_found", f"index not found: {index_uid}")
        await db.execute(text(f"DELETE FROM {DOC_TABLE} WHERE storage_id=:storage_id"), {"storage_id": row["storage_id"]})
        await db.execute(text(f"DELETE FROM {INDEX_TABLE} WHERE uid=:uid"), {"uid": index_uid})
        task = await _task(db, index_uid, "indexDeletion", {"storage_id": str(row["storage_id"])})
        await db.commit()
    return _task_response(task, index_uid, "indexDeletion", {"storage_id": str(row["storage_id"])})


async def _stats(index_uid: str) -> dict[str, Any]:
    async with async_session() as db:
        await _set_sql_budget_if_available(db)
        row = await _index(db, index_uid)
        if row is None: _not_found("index_not_found", f"index not found: {index_uid}")
        count = int((await db.execute(text(f"SELECT count(*) FROM {DOC_TABLE} WHERE storage_id=:storage_id"), {"storage_id": row["storage_id"]})).scalar_one())
        size = (await db.execute(text(f"SELECT pg_total_relation_size('{DOC_TABLE}')"))).scalar_one()
    return {"numberOfDocuments": count, "databaseSize": int(size or 0), "storage_id": str(row["storage_id"])}


async def _indexes(index_uid: str | None = None):
    async with async_session() as db:
        await _set_sql_budget_if_available(db)
        if index_uid is None:
            rows = (await db.execute(text(f"SELECT uid,storage_id,settings FROM {INDEX_TABLE} ORDER BY uid"))).mappings().all()
            return {"results": [dict(row) for row in rows], "offset": 0, "limit": len(rows), "total": len(rows)}
        row = (await db.execute(text(f"SELECT uid,storage_id,settings FROM {INDEX_TABLE} WHERE uid=:uid"), {"uid": index_uid})).mappings().one_or_none()
        if row is None:
            _not_found("index_not_found", f"index not found: {index_uid}")
        return dict(row)


async def _task_get(task_uid: int | None = None, *, limit: int = 20, offset: int = 0, status: str | None = None, index_uid: str | None = None) -> dict[str, Any]:
    async with async_session() as db:
        await _set_sql_budget_if_available(db)
        if task_uid is None:
            clauses = []
            params = {"limit": max(1, min(int(limit), 100)), "offset": max(0, int(offset))}
            if status:
                clauses.append("status=:status"); params["status"] = status
            if index_uid:
                clauses.append("index_uid=:index_uid"); params["index_uid"] = index_uid
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = (await db.execute(text(f"SELECT uid,index_uid,type,status,details,error,enqueued_at,started_at,finished_at FROM {TASK_TABLE}{where} ORDER BY uid DESC LIMIT :limit OFFSET :offset"), params)).mappings().all()
            return {"results": [_serialize_task(row) for row in rows], "limit": params["limit"], "from": params["offset"], "next": None}
        row = (await db.execute(text(f"SELECT uid,index_uid,type,status,details,error,enqueued_at,started_at,finished_at FROM {TASK_TABLE} WHERE uid=:uid"), {"uid": int(task_uid)})).mappings().one_or_none()
        if row is None: _not_found("task_not_found", f"task not found: {task_uid}")
    return _serialize_task(row)


def _serialize_task(row: Any) -> dict[str, Any]:
    value = dict(row)
    uid = int(value.pop("uid"))
    value["taskUid"] = uid
    value["uid"] = uid
    value["indexUid"] = value.pop("index_uid")
    for key in ("enqueued_at", "started_at", "finished_at"):
        timestamp = value.pop(key)
        value[{"enqueued_at":"enqueuedAt","started_at":"startedAt","finished_at":"finishedAt"}[key]] = timestamp.isoformat() if timestamp else None
    return value


async def request(client, method, path, deadline, payload=None, **kwargs):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("search adapter deadline exhausted")
    token = _DEADLINE.set(deadline)
    try:
        async with asyncio.timeout(remaining):
            return await _request_impl(client, method, path, deadline, payload, **kwargs)
    finally:
        _DEADLINE.reset(token)


async def _request_impl(client, method, path, deadline, payload=None, **kwargs):
    """Search-delivery-compatible request adapter; client is intentionally unused."""
    await _ensure()
    method = method.upper()
    path_only = str(path).split("?", 1)[0]
    if method in {"GET", "HEAD"} and path_only == "/indexes":
        return await _indexes()
    if method in {"GET", "HEAD"} and path_only.startswith("/indexes/") and path_only.endswith("/settings"):
        return await _settings(path_only.split("/")[2], "GET")
    if method == "GET" and path_only.startswith("/indexes/") and path_only.endswith("/stats"):
        return await _stats(path_only.split("/")[2])
    if method in {"GET", "HEAD"} and path_only.startswith("/indexes/") and path_only.count("/") == 2:
        return await _indexes(path_only.split("/")[2])
    if method == "GET" and path_only.startswith("/tasks"):
        split = urlsplit(str(path))
        tail = split.path.split("/")
        task_uid = int(tail[2]) if len(tail) > 2 and tail[2] else None
        if task_uid is not None:
            return await _task_get(task_uid)
        query = parse_qs(split.query)
        return await _task_get(None, limit=int(query.get("limit", ["20"])[0]),
                               offset=int(query.get("from", ["0"])[0]),
                               status=query.get("status", [None])[0],
                               index_uid=query.get("indexUid", [None])[0])
    if method == "POST" and path_only == "/indexes":
        return await _create_index(payload or {})
    if method == "PATCH" and path_only.startswith("/indexes/") and path_only.endswith("/settings"):
        return await _settings(path_only.split("/")[2], "PATCH", payload)
    if method == "POST" and path_only.startswith("/indexes/") and path_only.endswith("/documents"):
        return await _write_documents(path_only.split("/")[2], payload)
    if method in {"POST", "DELETE"} and path_only.startswith("/indexes/") and path_only.endswith("/documents/delete-batch"):
        return await _delete_documents(path_only.split("/")[2], payload)
    if method == "POST" and path_only == "/swap-indexes":
        return await _swap(payload)
    if method == "DELETE" and path_only.startswith("/indexes/"):
        return await _drop(path_only.split("/")[2])
    raise ValueError(f"Unsupported PG search protocol path: {method} {path}")




async def cleanup_tasks(db, cutoff, *, limit=500):
    """Bound command-history retention without deleting an unfinished receipt's proof."""
    result = await db.execute(text("""
        DELETE FROM search_pg_tasks WHERE uid IN (
            SELECT t.uid FROM search_pg_tasks t
            WHERE t.finished_at < :cutoff AND NOT EXISTS (
                SELECT 1 FROM search_delivery_receipts r
                WHERE r.backend = 'postgresql' AND r.task_uid=t.uid
                  AND r.state NOT IN ('complete','failed')
            ) ORDER BY t.finished_at,t.uid LIMIT :limit
        )
    """), {"cutoff": cutoff, "limit": max(1,min(500,int(limit)))})
    return int(result.rowcount or 0)
