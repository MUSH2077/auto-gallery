"""Durable search/dedup tail measurements in an isolated database.

Prepare fingerprints once; each timed case uses real outbox publication and RQ.
The controller samples only this cohort, without browsing or platform traffic.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import async_session, engine
from app.models import (
    Asset, AssetSource, Work, WorkSource, AssetDedupOutbox,
    AssetDedupCase, AssetDedupDecision, AssetDedupEvidence,
    VisualAssetGroup, VisualAssetMember, SearchProjectionOutbox,
    SearchDeliveryReceipt, AssetStorageState,
)
from app.services.media_derivatives import MEDIA_ALGORITHM_VERSION
from app.services.visual_image import PHASH_VERSION
from app.services.work_import import WorkImportService
from app.services.search_index_definitions import INDEX_SETTINGS, WORKS_INDEX
from app.services.search_projection_outbox import request_search_projection
from app.services.outbox_coordinator import mark_outbox_wake_pending

ROOT = Path("/tail")
MANIFEST = ROOT / "fixtures.json"


def isolated():
    if make_url(settings.database_url).database != "autogallery_tail_perf":
        raise RuntimeError("Tail mutation requires the isolated tail database")
    if not WORKS_INDEX.startswith("tail_"):
        raise RuntimeError("Tail mutation requires its own search namespace")


def client():
    return httpx.AsyncClient(base_url=settings.meili_url,
        headers={"Authorization": "Bearer " + settings.meili_master_key}, timeout=5)


async def remote(c, method, path, **kwargs):
    response = await c.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


async def terminal(c, uid):
    for _ in range(60):
        task = await remote(c, "GET", f"/tasks/{uid}")
        if task["status"] in ("succeeded", "failed", "canceled"):
            if task["status"] != "succeeded":
                raise RuntimeError("Fixture index preparation failed")
            return task
        await asyncio.sleep(.2)
    raise TimeoutError("Fixture index preparation")


async def prepare(payload):
    isolated()
    from PIL import Image
    inputs = ROOT / "inputs"
    inputs.mkdir(exist_ok=True)
    if payload.get("cohort"):
        cohort = json.loads((ROOT / payload["cohort"]).read_text())
        names = [item["template"] for item in cohort["assets"]]
        sources = [item["source"] for item in cohort["assets"]]
        expected_pairs = cohort["expected_pairs"]
    else:
        real = Path("/tail/real-small.png")
        with Image.open(real) as image:
            image.resize((800, 600)).convert("RGB").save(inputs / "real.png")
            image.resize((800, 600)).convert("RGB").save(inputs / "variant.jpg", quality=94)
            image.transpose(Image.Transpose.FLIP_LEFT_RIGHT).resize((800, 600)).convert("RGB").save(inputs / "other.png")
            image.transpose(Image.Transpose.FLIP_LEFT_RIGHT).resize((800, 600)).convert("RGB").save(inputs / "other-variant.jpg", quality=94)
            for stem, transform in [("third",Image.Transpose.FLIP_TOP_BOTTOM),("fourth",Image.Transpose.ROTATE_180)]:
                variant=image.transpose(transform).resize((800,600)).convert("RGB")
                variant.save(inputs / (stem+".png"));variant.save(inputs / (stem+".jpg"),quality=94)
        for index, color in enumerate([(0,0,0), (64,64,64), (190,190,190), (255,0,0)]):
            Image.new("RGB", (800,600), color).save(inputs / f"negative-{index}.png")
        names = ["real.png","real.png","other.png","other.png",
                 "third.png","third.jpg","fourth.png","fourth.jpg",
                 *[f"negative-{i}.png" for i in range(4)]]
        sources = ["pixiv","x","danbooru","manual","pixiv","x","danbooru","manual",
                   "pixiv","x","danbooru","manual"]
        expected_pairs = [
            {"indexes": [i, i + 1], "storage_state": "hardlinked" if i < 4 else "quarantined"}
            for i in (0, 2, 4, 6)
        ]
    assets = []
    async with async_session() as db:
        if (await db.execute(select(Work.id).limit(1))).first():
            raise RuntimeError("Fixture preparation requires an empty isolated database")
        for index, (name, source) in enumerate(zip(names, sources)):
            work = Work(title=f"tail fixture {index}")
            db.add(work); await db.flush()
            ws = WorkSource(work_id=work.id, source=source,
                source_work_id=f"tail-{index}", title=work.title, raw_metadata={})
            db.add(ws); await db.flush()
            path = Path(settings.download_root) / f"fixtures/{index}{Path(name).suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(inputs / name, path)
            stat = path.stat()
            with Image.open(path) as image:
                width, height = image.size
                mime_type = Image.MIME[image.format]
            asset = Asset(file_path=str(path.relative_to(settings.download_root)),
                file_name=path.name, file_size=stat.st_size, mime_type=mime_type,
                width=width, height=height, sha256=WorkImportService.sha256(path),
                phash=WorkImportService.phash(path, oriented=True), phash_version=PHASH_VERSION,
                derivative_version=MEDIA_ALGORITHM_VERSION,
                derivative_source_size=stat.st_size, derivative_source_mtime_ns=stat.st_mtime_ns)
            db.add(asset); await db.flush()
            work.thumbnail_asset_id=asset.id
            db.add(AssetSource(asset_id=asset.id, work_source_id=ws.id, source=source,
                source_asset_id=f"tail-asset-{index}", ordinal=0))
            assets.append({"id":str(asset.id),"work_id":str(work.id),"template":name,
                "file_path":asset.file_path,"mime":asset.mime_type,"width":width,"height":height,"bytes":stat.st_size})
        await db.commit()
    async with client() as c:
        for uid, spec in INDEX_SETTINGS.items():
            await terminal(c, (await remote(c,"POST","/indexes",json={"uid":uid,"primaryKey":"id"}))["taskUid"])
            await terminal(c, (await remote(c,"PATCH",f"/indexes/{uid}/settings",json=spec))["taskUid"])
    manifest={"assets":assets,"providers":[],"repositories":[],
        "expected_pairs":[{"asset_ids":[assets[i]["id"] for i in pair["indexes"]],
                           "storage_state":pair["storage_state"]} for pair in expected_pairs]}
    MANIFEST.write_text(json.dumps(manifest,indent=2))
    return {"assets":len(assets),"indexes_warm":True}


async def settle_previous(timeout=60):
    """Keep bootstrap/scheduled successors outside the next measured cohort."""
    from app.services.redis_client import get_redis
    from rq.queue import Queue
    from rq.registry import ScheduledJobRegistry, StartedJobRegistry

    connection = get_redis()
    queue = Queue("operations", connection=connection)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with async_session() as db:
            unfinished = await db.scalar(select(func.count()).select_from(SearchProjectionOutbox).where(
                SearchProjectionOutbox.completed_at.is_(None)))
            active = await db.scalar(select(func.count()).select_from(SearchDeliveryReceipt).where(
                SearchDeliveryReceipt.state.notin_(("complete", "failed"))))
        if (not unfinished and not active and queue.count == 0
                and StartedJobRegistry("operations", connection=connection).count == 0
                and ScheduledJobRegistry("operations", connection=connection).count == 0):
            return
        await asyncio.sleep(.5)
    raise TimeoutError("Previous search initialization or scheduled effects have not settled")


async def reset(manifest):
    isolated()
    ids=[UUID(a["id"]) for a in manifest["assets"]]
    async with async_session() as db:
        active=(await db.execute(select(AssetDedupOutbox.id).where(
            AssetDedupOutbox.state.in_(("pending","processing","failed")),
            (AssetDedupOutbox.event_type != "purge") | (AssetDedupOutbox.available_at <= datetime.now(timezone.utc))).limit(1))).first()
        active_search=(await db.execute(select(SearchDeliveryReceipt.id).where(
            SearchDeliveryReceipt.state.notin_(("complete","failed"))).limit(1))).first()
        if active or active_search:
            raise RuntimeError("Previous immediate effects have not settled")
        for model in [AssetDedupDecision,AssetDedupCase,VisualAssetMember,
                      VisualAssetGroup,AssetStorageState,AssetDedupEvidence,AssetDedupOutbox]:
            await db.execute(delete(model))
        for item, asset_id in zip(manifest["assets"], ids):
            path=Path(settings.download_root)/item["file_path"]
            path.parent.mkdir(parents=True,exist_ok=True)
            # copy2 writes through existing hardlinks. Recreate each original
            # independently so every case performs the same storage work.
            path.unlink(missing_ok=True)
            shutil.copy2(ROOT/"inputs"/item["template"],path)
            stat=path.stat()
            await db.execute(update(Asset).where(Asset.id==asset_id).values(
                derivative_source_size=stat.st_size,derivative_source_mtime_ns=stat.st_mtime_ns))
        await db.commit()


async def case(payload):
    isolated()
    manifest=json.loads(MANIFEST.read_text())
    await settle_previous()
    await reset(manifest)
    kind=payload.get("kind","mixed")
    label=payload["label"]
    ids=[UUID(a["id"]) for a in manifest["assets"]]
    works=[UUID(a["work_id"]) for a in manifest["assets"]]
    pairs = manifest.get("expected_pairs")
    if pairs is None:
        # Existing 12-asset manifests retain their known fixture expectations.
        pairs = [{"asset_ids":[str(x) for x in ids[i:i+2]],
                  "storage_state":"hardlinked" if i < 4 else "quarantined"}
                 for i in (0, 2, 4, 6)]
    expected_groups = {frozenset(UUID(x) for x in pair["asset_ids"]) for pair in pairs}
    grouped_ids = set().union(*expected_groups) if expected_groups else set()
    expected_state_by_group = {frozenset(UUID(x) for x in pair["asset_ids"]):pair["storage_state"] for pair in pairs}
    expected_states = {"available":len(pairs),
                       "hardlinked":sum(p["storage_state"]=="hardlinked" for p in pairs),
                       "quarantined":sum(p["storage_state"]=="quarantined" for p in pairs)}
    stamped=datetime.now(timezone.utc)
    started=time.perf_counter()
    async with async_session() as db:
        if kind in ("search","mixed"):
            for index, work in enumerate(works):
                await db.execute(update(Work).where(Work.id==work).values(title=f"{label} {index}"))
            await request_search_projection(db,works)
        if kind in ("dedup","mixed"):
            for item in manifest["assets"]:
                path=Path(settings.download_root)/item["file_path"];stat=path.stat()
                db.add(AssetDedupOutbox(idempotency_key=f"tail:{label}:{item['id']}",
                    event_type="observe",payload={"asset_id":item["id"],
                    "algorithm_version":MEDIA_ALGORITHM_VERSION,"source_size":stat.st_size,
                    "source_mtime_ns":stat.st_mtime_ns},state="pending",available_at=stamped))
            mark_outbox_wake_pending(db,"dedup")
        await db.commit()
    marks={}
    polls=0
    while time.perf_counter()-started < payload.get("timeout",120):
        async with async_session() as db:
            outbox=list((await db.execute(select(SearchProjectionOutbox).where(
                SearchProjectionOutbox.entity_id.in_([str(w) for w in works])))).scalars())
            effects=list((await db.execute(select(AssetDedupOutbox).where(
                (AssetDedupOutbox.event_type != "purge") | (AssetDedupOutbox.available_at <= datetime.now(timezone.utc))))).scalars())
            receipts=list((await db.execute(select(SearchDeliveryReceipt).where(
                SearchDeliveryReceipt.created_at>=stamped))).scalars())
        if any(e.state=="failed" for e in effects):
            raise RuntimeError("Dedup effect failed: "+"; ".join(e.last_error or "" for e in effects if e.state=="failed"))
        async with client() as c:
            if kind in ("search","mixed") and "visible_s" not in marks:
                docs=(await remote(c,"GET",f"/indexes/{WORKS_INDEX}/documents",params={"limit":len(works),"fields":"id,title"}))["results"]
                titles={d["id"]:d.get("title") for d in docs}
                if all(titles.get(str(w))==f"{label} {i}" for i,w in enumerate(works)):
                    marks["visible_s"]=time.perf_counter()-started
        if kind in ("search","mixed") and len(outbox)==len(works) and all(o.completed_at for o in outbox):
            marks.setdefault("acknowledged_s",time.perf_counter()-started)
        if kind in ("dedup","mixed") and len(effects)>=len(ids) and all(e.state=="complete" for e in effects):
            marks.setdefault("dedup_s",time.perf_counter()-started)
        done=(kind=="search" and "acknowledged_s" in marks and "visible_s" in marks) or (kind=="dedup" and "dedup_s" in marks) or (kind=="mixed" and len(marks)==3)
        polls+=1
        if done:
            async with async_session() as db:
                groups=list((await db.execute(select(VisualAssetMember.asset_id,VisualAssetGroup.representative_asset_id).join(VisualAssetGroup,VisualAssetMember.group_id==VisualAssetGroup.id))).all())
                storage=list((await db.execute(select(AssetStorageState))).scalars())
                counts = {model.__name__: (await db.scalar(select(func.count()).select_from(model)))
                          for model in (Work, Asset, AssetSource)}
            if counts != {name:len(ids) for name in ("Work", "Asset", "AssetSource")}:
                raise AssertionError("Tail processing changed the fixture identities")
            if kind in ("search", "mixed"):
                marks["ack_completed_s"] = max((o.completed_at-stamped).total_seconds() for o in outbox)
            if kind in ("dedup", "mixed"):
                marks["dedup_completed_s"] = max((e.completed_at-stamped).total_seconds() for e in effects)
                by_rep = {}
                for asset_id, representative in groups:
                    by_rep.setdefault(representative, set()).add(asset_id)
                if {frozenset(members) for members in by_rep.values()} != expected_groups:
                    raise AssertionError("Tail dedup groups differ from the known fixture pairs")
                by_id = {s.asset_id: s for s in storage}
                if len(effects) != len(ids) + len(pairs) or len(storage) != len(grouped_ids):
                    raise AssertionError("Tail dedup did not complete the expected observe/storage effects")
                items = {UUID(a["id"]): a for a in manifest["assets"]}
                for representative, members in by_rep.items():
                    rep_path = Path(settings.download_root)/items[representative]["file_path"]
                    if by_id[representative].storage_state != "available" or not rep_path.is_file():
                        raise AssertionError("Tail representative is unavailable")
                    for asset_id in members - {representative}:
                        state = by_id[asset_id]
                        if state.storage_state != expected_state_by_group[frozenset(members)]:
                            raise AssertionError("Tail duplicate storage action differs from its fixture pair")
                        path = Path(settings.download_root)/items[asset_id]["file_path"]
                        if state.served_by_asset_id != representative:
                            raise AssertionError("Tail media routing lost its representative")
                        if state.storage_state == "hardlinked":
                            if not path.exists() or not path.samefile(rep_path):
                                raise AssertionError("Tail exact duplicate was not hardlinked")
                            if state.bytes_reclaimed != items[asset_id]["bytes"]:
                                raise AssertionError("Tail exact duplicate did not reclaim its original bytes")
                        elif state.storage_state == "quarantined":
                            quarantine = Path(settings.download_root)/state.quarantine_path
                            if path.exists() or not quarantine.is_file():
                                raise AssertionError("Tail visual duplicate quarantine is incomplete")
                            if quarantine.read_bytes() != (ROOT/"inputs"/items[asset_id]["template"]).read_bytes():
                                raise AssertionError("Tail quarantine changed the original bytes")
                        else:
                            raise AssertionError("Tail duplicate has an unexpected storage state")
                for asset_id in set(ids) - grouped_ids:
                    path = Path(settings.download_root)/items[asset_id]["file_path"]
                    if asset_id in by_id or path.read_bytes() != (ROOT/"inputs"/items[asset_id]["template"]).read_bytes():
                        raise AssertionError("Tail unrelated image was changed by dedup")
                states = [s.storage_state for s in storage]
                if any(states.count(state) != count for state, count in expected_states.items()):
                    raise AssertionError("Tail dedup storage effects differ from the fixed cohort")
            if kind in ("search", "mixed"):
                async with client() as c:
                    hits = (await remote(c, "POST", f"/indexes/{WORKS_INDEX}/search", json={
                        "q": label, "limit": len(works), "attributesToRetrieve": ["id", "title"],
                    }))["hits"]
                titles = {h["id"]: h.get("title") for h in hits}
                if not all(titles.get(str(w)) == f"{label} {i}" for i, w in enumerate(works)):
                    raise AssertionError("Tail indexed titles do not match actual search results")
            from rq.job import Job
            from app.services.redis_client import get_redis
            redis=get_redis()
            rq_metrics=[]
            for key in redis.scan_iter(match="rq:job:outbox-*"):
                name=key.decode().removeprefix("rq:job:")
                job=Job.fetch(name,connection=redis)
                created=job.created_at
                if created and created.replace(tzinfo=timezone.utc)>=stamped:
                    rq_metrics.append({"id":name,"created":created.isoformat(),
                        "enqueued":job.enqueued_at.isoformat() if job.enqueued_at else None,
                        "started":job.started_at.isoformat() if job.started_at else None,
                        "ended":job.ended_at.isoformat() if job.ended_at else None,
                        "status":job.get_status()})
            task_metrics=[]
            async with client() as c:
                for receipt in receipts:
                    if receipt.task_uid is not None:
                        task=await remote(c,"GET",f"/tasks/{receipt.task_uid}")
                        task_metrics.append({**{k:task.get(k) for k in ["uid","status","enqueuedAt","startedAt","finishedAt","duration"]},
                            "local_receipt_updated":receipt.updated_at.isoformat(),"poll_count":receipt.poll_count})
            result={"kind":kind,"label":label,"marks":marks,"observer_polls":polls,
                "assets":len(ids),"effects":len(effects),"groups":[[str(a),str(r)] for a,r in groups],
                "storage":[{"asset_id":str(s.asset_id),"state":s.storage_state,"served_by":str(s.served_by_asset_id) if s.served_by_asset_id else None,"bytes_reclaimed":s.bytes_reclaimed} for s in storage],
                "remote_tasks":task_metrics,"rq_jobs":rq_metrics,"complete":True}
            (ROOT/"benchmarks"/f"{label}.json").write_text(json.dumps(result,indent=2))
            return result
        await asyncio.sleep(1)
    raise TimeoutError("Tail cohort did not finish")


async def main(payload):
    if payload["action"]=="prepare":return await prepare(payload)
    if payload["action"]=="case":return await case(payload)
    if payload["action"]=="settle":
        isolated()
        await settle_previous()
        return {"settled": True}
    raise ValueError("Unknown tail action")


if __name__=="__main__":
    import sys
    async def run():
        try:print(json.dumps(await main(json.load(sys.stdin))))
        finally:await engine.dispose()
    asyncio.run(run())
