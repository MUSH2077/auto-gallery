"""Opt-in upload registration timings; one aggregate record per save, no SQL values."""
import contextvars
import functools
import json
import time
from collections import Counter
from pathlib import Path

current = contextvars.ContextVar("upload_projection_trace", default=None)
stage = contextvars.ContextVar("upload_projection_stage", default="save")


def install(output):
    from sqlalchemy import event
    from app.database import engine
    from app.services.creator import CreatorService
    from app.services.manual_upload import ManualUploadService
    from app.services import search_projection_outbox as projection
    from sqlalchemy.ext.asyncio import AsyncSession

    def wrap(target, name, label):
        original = getattr(target, name)
        @functools.wraps(original)
        async def timed(*args, **kwargs):
            record = current.get()
            if record is None:
                return await original(*args, **kwargs)
            parent = stage.get()
            label_path = label if parent == "save" else parent + "/" + label
            token = stage.set(label_path)
            start = time.perf_counter()
            try:
                return await original(*args, **kwargs)
            finally:
                record["spans_s"][label_path] += time.perf_counter() - start
                stage.reset(token)
        setattr(target, name, timed)

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def before(conn, cursor, statement, parameters, context, executemany):
        if current.get() is not None:
            context.upload_projection_started = time.perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def after(conn, cursor, statement, parameters, context, executemany):
        record = current.get()
        if record is None or not hasattr(context, "upload_projection_started"):
            return
        verb = statement.lstrip().split(None, 1)[0].upper()
        key = stage.get() + ":" + verb
        record["sql_count"][key] += 1
        record["sql_s"][key] += time.perf_counter() - context.upload_projection_started
        if statement.lstrip().startswith("UPDATE users") and record["quota_return_at"] is None:
            record["quota_return_at"] = time.perf_counter()

    original_commit = AsyncSession.commit
    @functools.wraps(original_commit)
    async def commit(db):
        record = current.get()
        start = time.perf_counter()
        result = await original_commit(db)
        if record is not None:
            record["commit_s"] += time.perf_counter() - start
            if record["quota_return_at"] is not None and record["quota_to_commit_s"] is None:
                record["quota_to_commit_s"] = time.perf_counter() - record["quota_return_at"]
        return result
    AsyncSession.commit = commit

    wrap(CreatorService, "_request_creator_projection", "projection")
    wrap(CreatorService, "_projection_dependents", "dependencies")
    wrap(projection, "_fanout_memberships", "memberships")
    wrap(projection, "_enqueue_projection_action", "outbox")

    original = ManualUploadService.save_upload
    @functools.wraps(original)
    async def save(*args, **kwargs):
        record = {"event": "upload_projection", "sql_count": Counter(), "sql_s": Counter(),
                  "spans_s": Counter(), "commit_s": 0, "quota_return_at": None, "quota_to_commit_s": None}
        token = current.set(record)
        start = time.perf_counter()
        try:
            result = await original(*args, **kwargs)
            record["download_job_id"] = str(result.download_job_id)
            record["outcome"] = "ok"
            return result
        except BaseException:
            record["outcome"] = "error"
            raise
        finally:
            record["save_s"] = time.perf_counter() - start
            record.pop("quota_return_at")
            with Path(output).open("a") as stream:
                stream.write(json.dumps(record) + "\n")
            current.reset(token)
    ManualUploadService.save_upload = save
