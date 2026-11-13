"""Opt-in local benchmark tracing, loaded only by the temporary Compose overlay.

No business functions or return values are changed. SQL is counted/timed, not
logged. Arguments, cookies, settings values and gallery-dl commands are omitted.
"""
import os

if os.environ.get("AUTO_GALLERY_PERF_TRACE"):
    import contextvars
    import functools
    import inspect
    import json
    import resource
    import threading
    import time
    from datetime import datetime, timezone
    from pathlib import Path

    target = os.environ["AUTO_GALLERY_PERF_TRACE"]
    active = contextvars.ContextVar("local_perf_span", default=None)
    sample_pid = None

    def memory(pid=None):
        result = {}
        try:
            for line in Path(f"/proc/{pid or os.getpid()}/smaps_rollup").read_text().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    if key in {"Rss", "Pss", "Pss_Anon", "Pss_File", "Anonymous"}:
                        result[key + "_bytes"] = int(value.split()[0]) * 1024
        except (OSError, ValueError):
            pass
        return result

    def emit(event):
        event.update(at=datetime.now(timezone.utc).isoformat(), pid=os.getpid(), container=os.environ.get("HOSTNAME"))
        data = (json.dumps(event, separators=(",", ":"), default=str) + "\n").encode()
        fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def sampler():
        # Forked RQ workhorses get their own sampler on first traced operation.
        while True:
            emit({"event": "process_memory", **memory()})
            time.sleep(0.5)

    def start_sampler():
        global sample_pid
        if sample_pid != os.getpid():
            sample_pid = os.getpid()
            threading.Thread(target=sampler, daemon=True).start()

    def begin(stage, labels):
        start_sampler()
        parent = active.get()
        labels = {**(parent["labels"] if parent else {}), **labels}
        node = {"labels": labels, "stage": stage, "sql_count": 0, "sql_seconds": 0.0, "commits": 0}
        token = active.set(node)
        before = None if os.environ.get("AUTO_GALLERY_PERF_SPAN_MEMORY") == "0" or stage == "http" or stage.startswith("search.") else memory()
        return parent, node, token, time.perf_counter(), time.process_time(), resource.getrusage(resource.RUSAGE_CHILDREN), before

    def finish(stage, state, outcome):
        parent, node, token, wall, cpu, children, before = state
        duration = time.perf_counter() - wall
        child_end = resource.getrusage(resource.RUSAGE_CHILDREN)
        active.reset(token)
        if parent:
            for key in ("sql_count", "sql_seconds", "commits"):
                parent[key] += node[key]
        emit({"event": "span", "stage": stage, **node["labels"], "outcome": outcome,
              "wall_s": duration, "cpu_s": time.process_time() - cpu,
              "children_cpu_s": child_end.ru_utime + child_end.ru_stime - children.ru_utime - children.ru_stime,
              "sql_count": node["sql_count"], "sql_s": node["sql_seconds"], "commits": node["commits"],
              "memory_before": before, "memory_after": memory() if before is not None else None, "nested": parent is not None})

    def labels_for(stage, args):
        values = {}
        if stage in {"media.derive", "media.asset"}:
            first = next((a for a in args if isinstance(a, dict)), {})
            values = {k: str(first[k]) for k in ("asset_id", "width", "height", "file_size") if first.get(k) is not None}
            if first.get("file_path"):
                values["format"] = Path(first["file_path"]).suffix.lower()
        if stage.startswith("provider."):
            values["source"] = getattr(args[0], "source_name", "unknown") if args else "unknown"
        return values

    def wrap(owner, name, stage):
        original = getattr(owner, name, None)
        if original is None:
            return
        if inspect.iscoroutinefunction(original):
            @functools.wraps(original)
            async def traced(*args, **kwargs):
                state = begin(stage, labels_for(stage, args))
                outcome = "ok"
                try:
                    return await original(*args, **kwargs)
                except BaseException:
                    outcome = "error"
                    raise
                finally:
                    finish(stage, state, outcome)
        else:
            @functools.wraps(original)
            def traced(*args, **kwargs):
                state = begin(stage, labels_for(stage, args))
                outcome = "ok"
                try:
                    return original(*args, **kwargs)
                except BaseException:
                    outcome = "error"
                    raise
                finally:
                    finish(stage, state, outcome)
        if isinstance(owner, type) and isinstance(inspect.getattr_static(owner, name), staticmethod):
            traced = staticmethod(traced)
        setattr(owner, name, traced)

    def install():
        from sqlalchemy import event
        from sqlalchemy.orm import Session
        from app.database import engine

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def sql_begin(connection, cursor, statement, parameters, context, executemany):
            context._perf_started = time.perf_counter()

        @event.listens_for(engine.sync_engine, "after_cursor_execute")
        def sql_end(connection, cursor, statement, parameters, context, executemany):
            node = active.get()
            if node is not None:
                node["sql_count"] += 1
                node["sql_seconds"] += time.perf_counter() - context._perf_started

        @event.listens_for(Session, "after_commit")
        def commit(session):
            node = active.get()
            if node is not None:
                node["commits"] += 1

        from starlette.applications import Starlette
        original_call = Starlette.__call__

        async def call(app, scope, receive, send):
            headers = dict(scope.get("headers", []))
            case = headers.get(b"x-perf-case")
            if scope.get("type") != "http" or not case:
                return await original_call(app, scope, receive, send)
            state = begin("http", {"case": case.decode(), "operation": headers.get(b"x-perf-operation", b"").decode(),
                                   "path": scope["path"], "method": scope["method"]})

            async def annotated_send(message):
                if message["type"] == "http.response.start":
                    node = active.get()
                    message = {**message, "headers": [*message.get("headers", []),
                        (b"x-perf-sql-ms", str(node["sql_seconds"] * 1000).encode()),
                        (b"x-perf-sql-count", str(node["sql_count"]).encode()),
                        (b"x-perf-app-ms", str((time.perf_counter() - state[3]) * 1000).encode())]}
                await send(message)

            outcome = "ok"
            try:
                return await original_call(app, scope, receive, annotated_send)
            except BaseException:
                outcome = "error"
                raise
            finally:
                finish("http", state, outcome)

        Starlette.__call__ = call
        from rq.worker import Worker
        original_perform = Worker.perform_job

        @functools.wraps(original_perform)
        def perform(worker, job, *args, **kwargs):
            labels = {"rq_job_id": job.id, "function": job.func_name}
            # RQ arguments contain IDs here; do not serialize arbitrary job payloads.
            ids = [str(a) for a in job.args if len(str(a)) == 36 and str(a).count("-") == 4]
            if ids:
                labels["domain_job_id"] = ids[0]
            if job.enqueued_at:
                labels["queue_wait_s"] = max(0, (datetime.now(timezone.utc) - job.enqueued_at.replace(tzinfo=timezone.utc)).total_seconds())
            state = begin("rq.execute", labels)
            try:
                return original_perform(worker, job, *args, **kwargs)
            finally:
                finish("rq.execute", state, "finished")

        Worker.perform_job = perform
        from app.services import thumbnail, work_import, media_assets
        wrap(thumbnail, "inspect_and_generate_thumbnail", "media.thumbnail")
        wrap(media_assets, "inspect_video", "media.video_probe")
        wrap(media_assets, "_render_video_frames", "media.video_render")
        wrap(work_import.WorkImportService, "sha256", "media.sha256")
        wrap(work_import.WorkImportService, "phash", "media.phash")
        from app.services import media_derivatives, heavy_io, stage_metrics
        from app.services import outbox_coordinator
        original_release_wake = outbox_coordinator.wake_resource_deferred_outboxes
        def release_wake():
            at, cpu = time.perf_counter(), time.process_time()
            result = original_release_wake()
            if result.get("enqueued") or result.get("deferred"):
                # Runs in the RQ parent: emit without creating a sampler thread
                # before its next fork.
                emit({"event": "resource_release_wake", "wall_s": time.perf_counter() - at,
                      "cpu_s": time.process_time() - cpu, **result})
            return result
        outbox_coordinator.wake_resource_deferred_outboxes = release_wake
        original_reserve = heavy_io.RenewableRedisLease.try_acquire
        async def reserve(lease):
            result = await original_reserve(lease)
            if lease.reservation_key is not None:
                emit({"event": "resource_reservation", **(active.get() or {}).get("labels", {}),
                      "workload": lease.workload, "granted": result, "reason": lease.denial_reason,
                      "reserved_bytes": lease.reservation_bytes,
                      "capacity_bytes": lease.reservation_capacity_bytes})
            return result
        heavy_io.RenewableRedisLease.try_acquire = reserve
        original_claim = media_derivatives._claim_one
        @functools.wraps(original_claim)
        async def claimed(*args, **kwargs):
            at = time.perf_counter()
            result = await original_claim(*args, **kwargs)
            if result:
                emit({"event": "media_claimed", "asset_id": str(result["asset_id"]), "wall_s": time.perf_counter() - at,
                      **((active.get() or {}).get("labels", {}))})
            return result
        media_derivatives._claim_one = claimed
        for name, stage in [("_derive_sync", "media.derive"), ("_process_claimed_request", "media.asset"), ("_complete", "media.db_write")]:
            wrap(media_derivatives, name, stage)
        for name in ("wait_for_resource_capacity", "_wait_for_resource_event"):
            wrap(heavy_io, name, "resource." + name)
        original_slice = heavy_io.adaptive_resource_slice
        def admission(*args, **kwargs):
            manager = original_slice(*args, **kwargs)
            class MeasuredAdmission:
                async def __aenter__(self):
                    state = begin("resource.admission", {"workload": str(args[0] if args else kwargs.get("workload", "unknown"))})
                    outcome = "ok"
                    try:
                        result = await manager.__aenter__()
                        emit({"event": "resource_grant", **(active.get() or {}).get("labels", {}), "workload": str(args[0] if args else kwargs.get("workload", "unknown")),
                              "granted": result is not None,
                              **{key: getattr(result, key, None) for key in
                                 ("effective_scale", "governance_mode", "controller_mode", "work_units", "slice_seconds")}})
                        return result
                    except BaseException:
                        outcome = "error"
                        raise
                    finally:
                        finish("resource.admission", state, outcome)
                async def __aexit__(self, *values):
                    result = await manager.__aexit__(*values)
                    emit({"event": "resource_slice_release", **(active.get() or {}).get("labels", {}),
                          "workload": str(args[0] if args else kwargs.get("workload", "unknown")),
                          "cooldown_s": (kwargs.get("cooldown_result") or {}).get("seconds", 0)})
                    return result
            return MeasuredAdmission()
        heavy_io.adaptive_resource_slice = admission
        original_log = stage_metrics._log_metrics

        def log_metrics(payload):
            emit({"event": "native_stage", **payload})
            original_log(payload)

        stage_metrics._log_metrics = log_metrics
        from app.providers import registry
        for source in registry.list_sources():
            provider = registry.get(source)
            for name in ("parse_work_source", "parse_source_creator", "parse_source_tags"):
                # Instances would lose self binding. Patch each provider class once.
                wrap(type(provider), name, "provider." + name)
        from app.services.search import SearchService
        for name in ("search", "_search_meili", "_search_works_db", "_resolve_qualifiers"):
            wrap(SearchService, name, "search." + name)
        from app.services import manual_upload
        wrap(manual_upload, "_sniff_magic", "upload.validate")
        wrap(manual_upload.ManualUploadService, "_save_upload", "upload.save")
        from app.services import import_intent
        wrap(import_intent, "enqueue_import", "import.publish")
        from app.jobs import import_runner
        original_import = import_runner.run_import_job
        @functools.wraps(original_import)
        async def imported(job_id, *args, **kwargs):
            result = await original_import(job_id, *args, **kwargs)
            from app.database import async_session
            from app.models import ImportJob
            from uuid import UUID
            async with async_session() as db:
                stored = await db.get(ImportJob, UUID(str(job_id)))
                if stored is not None:
                    emit({"event": "import_persisted_status", "job_id": str(job_id), "status": stored.status})
            return result
        import_runner.run_import_job = imported
        from app.api import upload
        wrap(upload, "_stream_to_temp", "upload.spool_to_download_temp")
        wrap(upload, "_receive_upload_files", "upload.persist_and_publish")
        wrap(manual_upload.ManualUploadService, "_resolve_identity", "upload.identity")
        original_replace = os.replace
        def replaced(source, destination, *args, **kwargs):
            if "/manual/" not in str(destination) and "/.upload-tmp/" not in str(destination):
                return original_replace(source, destination, *args, **kwargs)
            state = begin("upload.move", {})
            try:
                return original_replace(source, destination, *args, **kwargs)
            finally:
                finish("upload.move", state, "ok")
        os.replace = replaced
        original_dump = json.dump
        def dumped(value, file, *args, **kwargs):
            if not str(getattr(file, "name", "")).endswith("metadata.json"):
                return original_dump(value, file, *args, **kwargs)
            state = begin("upload.metadata_write", {})
            try:
                return original_dump(value, file, *args, **kwargs)
            finally:
                finish("upload.metadata_write", state, "ok")
        json.dump = dumped
        emit({"event": "instrumentation_ready", "version": 1})

    def install_gallery():
        # This runs in the real gallery-dl subprocess, not a substitute downloader.
        import builtins
        import re
        import sys
        from urllib.parse import urlparse
        import requests
        destination = sys.argv[sys.argv.index("--destination") + 1] if "--destination" in sys.argv else None
        if not destination:
            return
        root = Path(destination).resolve()
        match = re.search(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", str(root))
        job_id = match.group(0) if match else "unknown"
        used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
        limit = int(os.environ.get("AUTO_GALLERY_PERF_DOWNLOAD_LIMIT", "1000000000"))
        original_open = builtins.open

        class BudgetFile:
            def __init__(self, file):
                self.file = file
            def __getattr__(self, name):
                return getattr(self.file, name)
            def __enter__(self):
                self.file.__enter__()
                return self
            def __exit__(self, *args):
                return self.file.__exit__(*args)
            def write(self, data):
                nonlocal used
                count = len(data.encode(self.file.encoding or "utf-8")) if isinstance(data, str) else len(data)
                if used + count > limit:
                    emit({"event": "download_budget_stop", "domain_job_id": job_id, "bytes": used, "limit": limit})
                    os._exit(74)
                at = time.perf_counter()
                result = self.file.write(data)
                used += count
                # Aggregate file write cost, rather than logging every network chunk.
                write_account["seconds"] += time.perf_counter() - at
                write_account["bytes"] += count
                return result

        write_account = {"seconds": 0.0, "bytes": 0}
        def budget_open(file, mode="r", *args, **kwargs):
            opened = original_open(file, mode, *args, **kwargs)
            if isinstance(file, (str, bytes, os.PathLike)) and any(v in mode for v in ("w", "a", "+")):
                if Path(file).resolve().is_relative_to(root):
                    return BudgetFile(opened)
            return opened
        builtins.open = budget_open
        original_request = requests.Session.request
        def network(session, method, url, *args, **kwargs):
            at = time.perf_counter()
            result = original_request(session, method, url, *args, **kwargs)
            emit({"event": "network", "domain_job_id": job_id, "host": urlparse(url).hostname,
                  "stream": bool(kwargs.get("stream")), "wall_s": time.perf_counter() - at, "status": result.status_code})
            return result
        requests.Session.request = network
        from gallery_dl.downloader.http import HttpDownloader
        for method in ("receive", "_receive_rate"):
            original = getattr(HttpDownloader, method)
            def make_receive(fn):
                def receive(self, *args, **kwargs):
                    at, before = time.perf_counter(), dict(write_account)
                    try:
                        return fn(self, *args, **kwargs)
                    finally:
                        emit({"event": "media_transfer", "domain_job_id": job_id,
                              "wall_s": time.perf_counter() - at,
                              "write_s": write_account["seconds"] - before["seconds"],
                              "bytes": write_account["bytes"] - before["bytes"]})
                return receive
            setattr(HttpDownloader, method, make_receive(original))
        import atexit
        atexit.register(lambda: emit({"event": "download_budget_total", "domain_job_id": job_id, "written_bytes": used}))
        emit({"event": "gallery_instrumentation_ready", "domain_job_id": job_id})

    try:
        import sys
        if Path(sys.argv[0]).name == "gallery-dl":
            install_gallery()
        else:
            install()
    except Exception as exc:
        # Fail visibly, while keeping Python startup diagnostics free of secrets.
        emit({"event": "instrumentation_error", "error_type": type(exc).__name__})
