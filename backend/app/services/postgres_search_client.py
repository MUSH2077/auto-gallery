"""Thread-bound adapter for the existing synchronous search SDK boundary."""
from __future__ import annotations
import asyncio, contextvars, functools, inspect, time
from types import SimpleNamespace
from datetime import datetime, timezone
from sqlalchemy import text

_CONTEXT = contextvars.ContextVar("postgres_search_context", default=None)


def _bridge(factory, timeout=120):
    context = _CONTEXT.get()
    if context is None:
        raise RuntimeError("Search SDK call must use run_search_call")
    if context["cancelled"]:
        raise asyncio.CancelledError
    async def bounded():
        if context["cancelled"]:
            raise asyncio.CancelledError
        task = asyncio.current_task()
        context["tasks"].add(task)
        try:
            async with asyncio.timeout(timeout):
                return await factory(context["db"])
        finally:
            context["tasks"].discard(task)
    future = asyncio.run_coroutine_threadsafe(bounded(), context["loop"])
    context["futures"].append(future)
    try:
        return future.result(timeout=timeout+1)
    except BaseException:
        future.cancel()
        raise


def _namespace(value):
    if isinstance(value, dict):
        result = dict(value)
        if "taskUid" in result: result["task_uid"] = result["taskUid"]
        if "numberOfDocuments" in result: result["number_of_documents"] = result["numberOfDocuments"]
        return SimpleNamespace(**result)
    return value

async def _request(method, path, payload=None):
    from app.services import postgres_search_store as store
    return await store.request(None, method, path, time.monotonic()+120, payload)

class PGIndex:
    primary_key = "id"
    def __init__(self, uid, revision=None, timeout=120):
        self.timeout=timeout
        self.uid = uid
        self.created_at = str(revision or uid)
    def search(self, query="", **kwargs):
        from app.services.postgres_search_query import search_one
        if "page" in kwargs:
            kwargs["limit"] = kwargs.pop("hits_per_page", 20)
            kwargs["offset"] = (kwargs.pop("page")-1)*kwargs["limit"]
        return _bridge(lambda db: search_one(db, self.uid, query, kwargs),self.timeout)
    def get_documents(self, *, fields=None, **kwargs):
        result = self.search("", attributes_to_retrieve=fields, **kwargs)
        return SimpleNamespace(results=result.hits, total=result.total_hits)
    def get_stats(self):
        return _namespace(_bridge(lambda db: _request("GET", f"/indexes/{self.uid}/stats")))
    def update_settings(self, settings):
        payload = settings.model_dump(by_alias=True, exclude_none=True) if hasattr(settings,"model_dump") else dict(settings)
        return _namespace(_bridge(lambda db: _request("PATCH", f"/indexes/{self.uid}/settings", payload)))
    def delete(self):
        return _namespace(_bridge(lambda db: _request("DELETE",f"/indexes/{self.uid}")))
    def update(self, **kwargs):
        return SimpleNamespace(task_uid=None)
    def add_documents(self, documents, **kwargs):
        return _namespace(_bridge(lambda db: _request("POST", f"/indexes/{self.uid}/documents", documents)))
    def delete_documents(self, ids):
        return _namespace(_bridge(lambda db: _request("POST", f"/indexes/{self.uid}/documents/delete-batch", ids)))
    def delete_document(self, identity): return self.delete_documents([identity])
    def delete_all_documents(self):
        raise RuntimeError("Use the bounded search rebuild to replace an index")

class PGClient:
    def __init__(self, **kwargs):self.timeout=float(kwargs.get("timeout_seconds") or 120)
    def index(self, uid): return PGIndex(uid,timeout=self.timeout)
    def get_index(self, uid):
        row = _bridge(lambda db: _request("GET", f"/indexes/{uid}"))
        return PGIndex(uid, row["storage_id"],self.timeout)
    def create_index(self, uid, primary_key="id"):
        return _namespace(_bridge(lambda db: _request("POST", "/indexes", {"uid":uid,"primaryKey":primary_key})))
    def delete_index(self, uid):
        return _namespace(_bridge(lambda db: _request("DELETE", f"/indexes/{uid}")))
    def wait_for_task(self, uid, **kwargs):
        return _namespace(_bridge(lambda db: _request("GET", f"/tasks/{uid}")))
    def multi_search(self, requests):
        from app.services.postgres_search_query import search_one
        async def run(db):
            values=[]
            for value in requests:
                kwargs=value.model_dump(by_alias=False, exclude_none=True)
                uid=kwargs.pop("index_uid",None) or kwargs.pop("indexUid",None)
                query=kwargs.pop("query",kwargs.pop("q",""))
                if "page" in kwargs:
                    kwargs["limit"]=kwargs.pop("hits_per_page",20)
                    kwargs["offset"]=(kwargs.pop("page")-1)*kwargs["limit"]
                values.append(await search_one(db,uid,query,kwargs))
            return values
        return _bridge(run,self.timeout)

async def run_search_call(function, *args, db=None, **kwargs):
    """Do not release a request's AsyncSession while a bridged query still runs."""
    context = {"loop":asyncio.get_running_loop(), "db":db, "tasks":set(), "futures":[], "cancelled":False}
    token = _CONTEXT.set(context)
    operation = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        context["cancelled"] = True
        for future in list(context["futures"]):
            future.cancel()
        tasks = list(context["tasks"])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await operation
        except BaseException:
            pass
        raise
    finally:
        _CONTEXT.reset(token)
