#!/usr/bin/env python3
"""Isolated NAS acceptance driver; never imports pytest or disables DB durability.

Run through scripts/latency-acceptance.py. Every trial creates distinct provider
identities. Cache categories refer to application reads; host caches stay intact.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import multiprocessing
import os
from pathlib import Path
import random
import time
from uuid import UUID, uuid4

from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import async_session, engine


class QueryCount:
    """Flat observer independent of either revision's nested metric behavior."""
    def __enter__(self):
        self.sql = self.commits = 0
        event.listen(engine.sync_engine, "before_cursor_execute", self.statement)
        event.listen(Session, "after_commit", self.commit)
        return self

    def statement(self, *_args, **_kwargs):
        self.sql += 1

    def commit(self, session):
        if not session.in_nested_transaction():
            self.commits += 1

    def __exit__(self, *_args):
        event.remove(engine.sync_engine, "before_cursor_execute", self.statement)
        event.remove(Session, "after_commit", self.commit)


def guard():
    if os.environ.get("LATENCY_ACCEPTANCE") != "isolated-nas-v1":
        raise RuntimeError("This driver requires the isolated NAS acceptance environment")
    if "@latency-postgres:" not in settings.database_url or "/latency_" not in settings.database_url:
        raise RuntimeError("Refusing a non-acceptance database")
    for root in (settings.download_root, settings.library_root):
        if not Path(root).is_relative_to("/latency-data"):
            raise RuntimeError("Refusing a non-acceptance filesystem root")
    if not Path("/latency-data/.latency-acceptance").is_file():
        raise RuntimeError("Missing acceptance filesystem marker")


async def seed():
    from app.models.user import User
    from app.auth import hash_password

    statements = [
        """INSERT INTO works (id,title,is_nsfw,is_ai_generated,is_favorite)
           SELECT md5('work-'||g)::uuid,'Acceptance work '||g,false,false,false
           FROM generate_series(1,75000) g ON CONFLICT DO NOTHING""",
        """INSERT INTO tags (id,normalized_name,category)
           SELECT md5('tag-'||g)::uuid,'acceptance-tag-'||g,'general'
           FROM generate_series(1,37000) g ON CONFLICT DO NOTHING""",
        """INSERT INTO creators (id,name,is_active,is_favorite)
           SELECT md5('creator-'||g)::uuid,'Acceptance creator '||g,true,false
           FROM generate_series(1,1500) g ON CONFLICT DO NOTHING""",
        """INSERT INTO source_creators (id,creator_id,source,source_creator_id,display_name)
           SELECT md5('sc-'||g)::uuid,md5('creator-'||g)::uuid,'pixiv',g::text,'Acceptance creator '||g
           FROM generate_series(1,1500) g ON CONFLICT DO NOTHING""",
        """INSERT INTO work_sources (id,work_id,source,source_work_id,source_creator_id,title,raw_metadata)
           SELECT md5('ws-'||g)::uuid,md5('work-'||g)::uuid,'pixiv',g::text,
                  ((g-1)%1500+1)::text,'Acceptance work '||g,'{}'::jsonb
           FROM generate_series(1,75000) g ON CONFLICT DO NOTHING""",
        """INSERT INTO work_tags (id,work_id,tag_id,source)
           SELECT md5('wt-'||g||'-'||t)::uuid,md5('work-'||g)::uuid,
                  md5('tag-'||((g*6+t)%37000+1))::uuid,'acceptance'
           FROM generate_series(1,75000) g CROSS JOIN generate_series(0,5) t ON CONFLICT DO NOTHING""",
        """INSERT INTO assets (id,file_path,file_name,file_size,mime_type,width,height)
           SELECT md5('asset-'||g)::uuid,'padding/'||g||'.jpg',g||'.jpg',1024,'image/jpeg',1300,1900
           FROM generate_series(1,90000) g ON CONFLICT DO NOTHING""",
        """INSERT INTO storage_artifacts
           (id,storage_root,file_path,source,creator_dir,source_work_id,file_name,artifact_type,file_size,state,attempts)
           SELECT md5('artifact-'||g)::uuid,'downloads','padding/'||g||'.json','pixiv','padding',
                  g::text,g||'.json','metadata_json',512,'done',0
           FROM generate_series(1,330000) g ON CONFLICT DO NOTHING""",
    ]
    async with async_session() as db:
        if (await db.execute(text("SHOW synchronous_commit"))).scalar_one() != "on":
            raise RuntimeError("Acceptance requires synchronous_commit=on")
        for statement in statements:
            await db.execute(text(statement))
            await db.commit()
        if not (await db.execute(select(User).where(User.username == "latency-bench"))).scalar_one_or_none():
            db.add(User(username="latency-bench", password_hash=hash_password("acceptance-local-only"),
                        is_admin=True, is_active=True, nsfw_visible=True))
            await db.commit()
        await db.execute(text("ANALYZE"))
        await db.commit()
        counts = {name: (await db.execute(text(f"SELECT count(*) FROM {name}"))).scalar_one()
                  for name in ("works", "tags", "work_tags", "assets", "storage_artifacts")}
    print(json.dumps({"event": "seeded", "counts": counts, "synchronous_commit": "on"}), flush=True)


def media_fixture(stage, work_count, asset_count, identity):
    from PIL import Image

    # Varied image pixels and 1300x1900 resolution exercise decode/thumbnail CPU.
    # A deterministic texture avoids the unrealistic all-solid tiny JPEG case.
    rng = random.Random(identity)
    texture = Image.frombytes("RGB", (325, 475), rng.randbytes(325 * 475 * 3)).resize((1300, 1900))
    import io
    data = io.BytesIO()
    texture.save(data, format="JPEG", quality=90)
    payload = data.getvalue()
    entries = []
    for work in range(work_count):
        work_id = str(identity * 100 + work)
        pages = asset_count // work_count + (work < asset_count % work_count)
        folder = stage.root / "pixiv" / str(identity) / work_id
        folder.mkdir(parents=True, exist_ok=True)
        for page in range(pages):
            image = folder / f"{work_id}_p{page}.jpg"
            image.write_bytes(payload)
            raw = {"id": int(work_id), "title": f"Latency work {work}", "num": page,
                   "user": {"id": identity, "name": str(identity), "account": str(identity)},
                   "date": "2026-09-01T00:00:00+00:00", "description": "NAS latency acceptance",
                   "page_count": pages, "width": 1300, "height": 1900,
                   "url": f"https://example.invalid/{work_id}_p{page}.jpg",
                   "tags": [f"acceptance-tag-{index}" for index in range(1, 15)]}
            metadata = image.with_suffix(".jpg.json")
            metadata.write_text(json.dumps(raw), encoding="utf-8")
            entries.extend((image, metadata))
    return entries, hashlib.sha256(payload).hexdigest(), len(payload)


async def browse(stop, samples, *, once=False):
    import httpx
    from app.auth import create_access_token

    async with httpx.AsyncClient(base_url=os.environ["LATENCY_BROWSE_URL"], timeout=30) as client:
        headers = {"Authorization": "Bearer " + create_access_token("latency-bench")}
        while not stop.is_set():
            started = time.perf_counter()
            response = await client.get("/api/v1/works", params={"limit": 50}, headers=headers)
            samples.append({"seconds": time.perf_counter() - started, "status": response.status_code})
            if response.status_code != 200:
                raise RuntimeError(f"Browse failed: {response.status_code} {response.text[:300]}")
            if once:
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
            except TimeoutError:
                pass


async def monitor_pressure(stop, samples):
    from app.services.resource_pressure import sample_and_publish_resource_pressure

    while not stop.is_set():
        # This process is the ordinary sampler for this isolated worker; reading
        # the cached snapshot would otherwise reuse a sample for up to 40s.
        snapshot = await asyncio.to_thread(sample_and_publish_resource_pressure)
        budget = snapshot.get("budget") or {}
        samples.append({"at": time.time(), "sampled_at": snapshot.get("sampled_at"), "status": snapshot.get("status"),
                        "controller_mode": snapshot.get("controller_mode"),
                        "reasons": snapshot.get("reasons"),
                        "budget": {key: budget.get(key) for key in ("generation", "effective_throughput_scale", "reservation")},
                        "resource_sample": {key: snapshot.get(key) for key in ("memory", "swap", "psi", "cgroup_memory_events")}})
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(1.0, settings.resource_pressure_sample_interval_seconds))
        except TimeoutError:
            pass


def observe_worker(directory, finished, phase, enable_browse):
    """Independent process: importer event-loop stalls cannot delay probes."""
    async def observe():
        stop = asyncio.Event()
        pressure, samples, warmup = [], [], []
        monitoring = asyncio.create_task(monitor_pressure(stop, pressure))
        while not pressure:
            if monitoring.done():
                await monitoring
            await asyncio.sleep(0.02)
        if enable_browse:
            await browse(stop, warmup, once=True)

        async def observe_browse():
            while not stop.is_set() and phase.value != 3:
                await asyncio.sleep(0.02)
            if not stop.is_set() and enable_browse:
                await browse(stop, samples)

        browsing = asyncio.create_task(observe_browse())
        (directory / "ready").touch()
        await asyncio.to_thread(finished.wait)
        stop.set()
        await asyncio.gather(monitoring, browsing)
        (directory / "observations.json").write_text(json.dumps({
            "pressure_samples": pressure, "browse": samples, "browse_warmup": warmup,
            "sample_interval_seconds": max(1.0, settings.resource_pressure_sample_interval_seconds),
            "probe_process": "separate_process_shared_worker_cpu_cap",
        }))

    asyncio.run(observe())


class TrialObservation:
    def __init__(self, identity, enable_browse):
        context = multiprocessing.get_context("spawn")
        self.directory = Path(settings.download_root) / ".latency-observations" / str(identity)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.finished = context.Event()
        self.phase = context.Value("i", 0)
        self.process = context.Process(target=observe_worker, args=(
            self.directory, self.finished, self.phase, enable_browse))
        self.boundaries = {}

    async def __aenter__(self):
        self.process.start()
        while not (self.directory / "ready").exists():
            if not self.process.is_alive():
                raise RuntimeError("Acceptance observer failed before measurement")
            await asyncio.sleep(0.05)
        return self

    def enter(self, name, phase):
        self.boundaries[name] = time.time()
        self.phase.value = phase

    async def __aexit__(self, *_args):
        self.boundaries["import_finished"] = time.time()
        self.finished.set()
        await asyncio.to_thread(self.process.join)
        if self.process.exitcode != 0:
            raise RuntimeError(f"Acceptance observer failed: {self.process.exitcode}")
        self.result = json.loads((self.directory / "observations.json").read_text())
        self.result["phase_boundaries"] = self.boundaries


async def trial(args):
    from app.models import Asset, Creator, DownloadJob, ImportJob, Subscription, SubscriptionSource, WorkSource, AssetSource
    from app.providers import registry
    from app.services.download_staging import DownloadStage
    from app.services.artifact_discovery import group_metadata_by_work, media_files_for_group
    from app.services.artifact_ledger import ArtifactLedger, artifact_row
    from app.services.stage_metrics import measure_stage
    from app.jobs.download import _enqueue_import
    from app.jobs.import_runner import run_import_job
    from app.services.redis_client import get_redis
    from app.models.task_run import TaskRun

    job_id = uuid4()
    identity = args.identity
    source_url = f"https://www.pixiv.net/users/{identity}"
    async with async_session() as db:
        if (await db.execute(select(WorkSource.id).where(WorkSource.source == "pixiv", WorkSource.source_creator_id == str(identity)).limit(1))).first():
            raise RuntimeError("This provider identity was already imported; use a fresh trial identity")
        creator = Creator(name=str(identity))
        db.add(creator)
        await db.flush()
        subscription = Subscription(creator_id=creator.id, name="Latency acceptance", schedule_mode="manual", sync_enabled=False)
        db.add(subscription)
        await db.flush()
        source = SubscriptionSource(subscription_id=subscription.id, source="pixiv", source_creator_id=str(identity), source_url=source_url)
        db.add(source)
        await db.flush()
        job = DownloadJob(id=job_id, subscription_id=subscription.id, subscription_source_id=source.id,
                          source="pixiv", source_url=source_url, status="downloaded")
        db.add(job)
        await db.commit()
    root = Path(settings.download_root)
    stage = DownloadStage.open(root, str(job_id), "pixiv")
    entries, image_sha, image_bytes = media_fixture(stage, args.works, args.assets, identity)
    provider = registry.get("pixiv")
    async with TrialObservation(identity, args.browse) as observation:
        observation.enter("promotion_started", 1)
        started = time.perf_counter()
        with QueryCount() as promotion_queries, measure_stage("acceptance_promotion_registration", files=len(entries)) as promotion_metric:
            promotion = stage.promote(provider=provider)
            paths = set(promotion.paths)
            groups, invalid = group_metadata_by_work(provider, sorted(p for p in paths if p.suffix == ".json"))
            if invalid or len(groups) != args.works:
                raise RuntimeError(f"Fixture parse mismatch: {len(groups)} groups, {invalid}")
            rows = []
            for work_id, items in groups.items():
                for path in [p for p, _ in items] + media_files_for_group(items, work_id, allowed_paths=paths):
                    rows.append(artifact_row(path, root, job_id, source="pixiv", source_work_id=work_id))
            async with async_session() as db:
                await ArtifactLedger(db).upsert_many(rows)
                await db.commit()
            stage.mark_registered()
        promotion_seconds = time.perf_counter() - started
        promotion_metric.update(observed_sql_count=promotion_queries.sql, observed_durable_commits=promotion_queries.commits)
        observation.enter("creation_started", 2)
        started = time.perf_counter()
        import_id = await _enqueue_import(str(job_id), new_json_paths={str(p) for p in paths if p.suffix == ".json"})
        creation_seconds = time.perf_counter() - started
        if not import_id:
            raise RuntimeError("Import intent was not created")
        async with async_session() as db:
            task = (await db.execute(select(TaskRun).where(TaskRun.subject_type == "import_job", TaskRun.subject_id == UUID(str(import_id))))).scalar_one()
            # Dequeue the actual published job before driving its ordinary coroutine.
            # No worker runs in this fixture; the measured region is worker execution.
            from rq import Queue
            Queue("imports", connection=get_redis()).remove(task.rq_job_id)
        observation.enter("import_started", 3)
        started = time.perf_counter()
        with QueryCount() as import_queries, measure_stage("acceptance_import", works=args.works, assets=args.assets) as import_metric:
            await run_import_job(str(import_id))
        import_seconds = time.perf_counter() - started
        import_metric.update(observed_sql_count=import_queries.sql, observed_durable_commits=import_queries.commits)
    pressure = observation.result["pressure_samples"]
    async with async_session() as db:
        imported = await db.get(ImportJob, UUID(str(import_id)))
        ids = list((await db.execute(select(WorkSource.id).where(WorkSource.source == "pixiv", WorkSource.source_creator_id == str(identity)))).scalars())
        asset_count = (await db.execute(select(func.count()).select_from(AssetSource).where(AssetSource.work_source_id.in_(ids)))).scalar_one()
        if imported.status != "complete" or len(ids) != args.works or asset_count != args.assets:
            raise RuntimeError(f"Import mismatch: status={imported.status}, works={len(ids)}, assets={asset_count}, error={imported.error_log}")
        asset_paths = list((await db.execute(select(Asset.file_path).join(AssetSource, AssetSource.asset_id == Asset.id)
                                            .where(AssetSource.work_source_id.in_(ids)))).scalars())
        result = {"event": "trial", "variant": args.variant, "repetition": args.repetition,
                  "source_revision": os.environ.get("LATENCY_SOURCE_REVISION"),
                  "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  "orchestrator_sha256": os.environ.get("LATENCY_ORCHESTRATOR_SHA256"),
                  "run_id": os.environ.get("LATENCY_RUN_ID"),
                  "category": "new", "read_category": "freshly_written_media_natural_db_cache", "warmup": args.repetition == 0,
                  "works": len(ids), "assets": asset_count, "files": len(entries), "image_bytes": image_bytes,
                  "new_works": len(ids), "updated_works": 0, "skipped_works": 0,
                  "image_sha256": image_sha, "creation_seconds": creation_seconds, "promotion_seconds": promotion_seconds,
                  "import_seconds": import_seconds, **observation.result,
                  "job_id": str(job_id), "import_id": str(import_id),
                  "progress": imported.progress_data, "promotion_metrics": promotion_metric, "import_metrics": import_metric}
        result["pressure_samples"] = pressure
        result["normal_resource"] = bool(pressure) and all(s["status"] != "paused" and s["controller_mode"] != "critical" for s in pressure)
    for relative in asset_paths:
        asset_path = (Path(settings.download_root) / relative).resolve()
        if not asset_path.is_relative_to(Path(settings.download_root).resolve()):
            raise RuntimeError("Imported asset escapes the isolated downloads")
        with asset_path.open("rb") as handle:
            matches = hashlib.file_digest(handle, "sha256").hexdigest() == image_sha
        if not matches:
            raise RuntimeError(f"Imported media content changed: {relative}")
    result["verified_media_sha256_count"] = len(asset_paths)
    print(json.dumps(result, default=str), flush=True)


async def pending_import(args):
    """Real 120-second pending interval, controlled HTTP, full ordinary importer."""
    import httpx
    from app.models.search_projection_outbox import SearchProjectionOutbox
    from app.services.search import INDEX_SETTINGS, WORKS_INDEX
    from app.services.search_delivery import run_delivery_slice
    from app.services.search_projection_outbox import request_search_projection
    from app.services.remote_search_flight import read_marker
    from app.services.heavy_io import local_lock_for_workload
    from app.models.search_delivery_receipt import SearchDeliveryReceipt

    identity = uuid4()
    async with async_session() as db:
        if (await db.execute(select(SearchProjectionOutbox.id).where(SearchProjectionOutbox.completed_at.is_(None)).limit(1))).first():
            raise RuntimeError("Pending acceptance needs a fresh clone with no pre-existing search backlog")
        await request_search_projection(db, deleted_work_ids=[identity])
        await db.commit()
        target = (await db.execute(select(SearchProjectionOutbox).where(SearchProjectionOutbox.entity_id == str(identity)))).scalar_one()
        target_version = [str(target.id), target.version]
    remote_started = None
    writes, timings = [], []

    def remote(request):
        nonlocal remote_started
        if request.method == "GET" and "/tasks/" in request.url.path:
            status = "succeeded" if time.monotonic() - remote_started >= 120 else "processing"
            return httpx.Response(200, json={"uid": 71001, "status": status})
        if request.method == "GET" and request.url.path.endswith("/settings"):
            return httpx.Response(200, json=INDEX_SETTINGS[WORKS_INDEX])
        if request.method == "GET":
            return httpx.Response(200, json={"uid": WORKS_INDEX, "primaryKey": "id"})
        writes.append(request.url.path)
        remote_started = time.monotonic()
        return httpx.Response(202, json={"taskUid": 71001})

    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        started = time.monotonic()
        result = await run_delivery_slice(client=client)
        timings.append(time.monotonic() - started)
        if result["status"] != "pending" or read_marker() is None:
            raise RuntimeError(f"Pending fixture did not submit: {result}")
        async with async_session() as db:
            receipt = (await db.execute(select(SearchDeliveryReceipt).where(SearchDeliveryReceipt.task_uid == 71001))).scalar_one()
            if receipt.versions != [target_version]:
                raise RuntimeError("Pending acceptance selected an unrelated outbox version")
        maintenance = local_lock_for_workload("maintenance")
        acquired = maintenance.try_acquire()
        if acquired:
            maintenance.release()
            raise RuntimeError("Maintenance entered during remote flight")
        await trial(args)
        import_completed_after = time.monotonic() - remote_started
        if import_completed_after >= 120 or read_marker() is None:
            raise RuntimeError("Small import did not finish during the pending interval")
        while result["status"] != "ok":
            await asyncio.sleep(min(15, max(0.1, result["successor_delay_seconds"])))
            started = time.monotonic()
            result = await run_delivery_slice(client=client)
            timings.append(time.monotonic() - started)
            if time.monotonic() - remote_started > 150:
                raise RuntimeError(f"Pending receipt did not settle: {result}")
        if max(timings) > 20:
            raise RuntimeError(f"A pending delivery call exceeded the 20s local budget: {timings}")
        async with async_session() as db:
            row = (await db.execute(select(SearchProjectionOutbox).where(SearchProjectionOutbox.entity_id == str(identity)))).scalar_one()
            if row.completed_at is None or row.attempts or read_marker() is not None or len(writes) != 1:
                raise RuntimeError("Pending delivery lost acknowledgment/fencing invariants")
    print(json.dumps({"event": "pending_import", "remote_pending_seconds": 120,
                      "import_completed_after_seconds": import_completed_after,
                      "delivery_call_seconds": timings, "remote_writes": len(writes),
                      "target_outbox_version": target_version,
                      "acknowledged": True, "http_mode": "controlled", "database_mode": "real NAS PostgreSQL"}), flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("seed", "trial", "pending"))
    parser.add_argument("--variant", default="candidate")
    parser.add_argument("--works", type=int, default=9)
    parser.add_argument("--assets", type=int, default=27)
    parser.add_argument("--identity", type=int, default=9000001)
    parser.add_argument("--repetition", type=int, default=0)
    parser.add_argument("--browse", action="store_true")
    args = parser.parse_args()
    guard()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        await {"seed": seed, "trial": lambda: trial(args), "pending": lambda: pending_import(args)}[args.command]()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
