"""Read-only fixture and per-job inspection inside the existing backend image."""
import asyncio
import json
from pathlib import Path
import sys
from uuid import UUID

from sqlalchemy import select
from app.database import async_session, engine
from app.config import settings
from app.models import Asset, AssetSource, WorkSource, Work, MediaDerivativeOutbox, DownloadJob, ImportJob, SubscriptionSource, Subscription, Creator, StorageArtifact


def stamp(obj, field):
    value = getattr(obj, field)
    return value.isoformat() if value else None


async def main(request):
    async with async_session() as db:
        if request["action"] == "preview":
            import subprocess
            from app.providers import registry
            source = request["source"]
            known = set((await db.execute(select(WorkSource.source_work_id).where(WorkSource.source == source))).scalars())
            result = await asyncio.to_thread(subprocess.run,
                ["gallery-dl", "--config", "/gallerydl-config/config.json", "--resolve-json", "--range", "1-100", request["url"]],
                capture_output=True, text=True, timeout=180)
            if result.returncode:
                raise RuntimeError("Read-only source preview failed, exit " + str(result.returncode))
            choices = {}
            for row in json.loads(result.stdout):
                if not isinstance(row, list) or not isinstance(row[-1], dict):
                    continue
                raw = row[-1]
                identity = str(registry.get(source).parse_work_source(raw).get("source_work_id") or "")
                extension = str(raw.get("extension") or "").lower()
                if not identity or identity in known or extension not in {"jpg", "jpeg", "png", "gif", "webp", "mp4", "webm"}:
                    continue
                choices[identity] = {"source_work_id": identity, "extension": extension,
                    "source_creator_id": str(registry.get(source).parse_source_creator(raw).get("source_creator_id") or "")}
            return {"source": source, "choices": list(choices.values())[:3], "known_works": len(known)}
        if request["action"] == "inventory":
            return {"work_ids": [str(v) for v in (await db.execute(select(Work.id))).scalars()],
                    "asset_ids": [str(v) for v in (await db.execute(select(Asset.id))).scalars()]}
        if request["action"] == "imports":
            ids = [UUID(v) for v in request["ids"]]
            rows = (await db.execute(select(ImportJob).where(ImportJob.id.in_(ids)))).scalars().all()
            return {"items": [{"id": str(i.id), "status": i.status, "metrics": (i.progress_data or {}).get("phase_timings_ms")} for i in rows]}
        if request["action"] == "fixtures":
            rows = (await db.execute(select(Asset, WorkSource).join(AssetSource, AssetSource.asset_id == Asset.id)
                       .join(WorkSource, WorkSource.id == AssetSource.work_source_id)
                       .where(WorkSource.source.in_(("pixiv", "x", "danbooru", "manual"))))).all()
            assets = {}
            providers = {}
            for asset, work in rows:
                if not asset.thumb_sm_path or not (Path(settings.download_root) / asset.file_path).is_file():
                    continue
                assets[str(asset.id)] = {"id": str(asset.id), "file_path": asset.file_path, "bytes": asset.file_size,
                    "width": asset.width, "height": asset.height, "mime": asset.mime_type, "source": work.source,
                    "work_id": str(work.work_id), "sha256": bool(asset.sha256), "phash": bool(asset.phash)}
                providers.setdefault(work.source, {"source": work.source, "source_work_id": work.source_work_id,
                    "raw_metadata": work.raw_metadata, "work_id": str(work.work_id), "file_path": asset.file_path})
            manual = (await db.execute(select(WorkSource).where(WorkSource.source == "manual").limit(1))).scalar_one_or_none()
            if manual:
                providers["manual"] = {"source": "manual", "source_work_id": manual.source_work_id,
                    "raw_metadata": manual.raw_metadata, "work_id": str(manual.work_id)}
            sources = (await db.execute(select(SubscriptionSource, Subscription, Creator)
                       .join(Subscription, Subscription.id == SubscriptionSource.subscription_id)
                       .join(Creator, Creator.id == Subscription.creator_id)
                       .where(SubscriptionSource.source.in_(("pixiv", "x", "danbooru"))))).all()
            repositories = [{"id": str(s.id), "source": s.source, "url": s.source_url,
                             "creator_id": str(c.id), "creator_name": c.name, "subscription_id": str(sub.id)}
                            for s, sub, c in sources]
            return {"assets": list(assets.values()), "providers": list(providers.values()), "repositories": repositories}
        if request["action"] == "upload_batch":
            identities = [item["work_source_id"] for item in request["items"]]
            job_ids = [UUID(item["job_id"]) for item in request["items"]]
            jobs = {str(j.id): j for j in (await db.execute(select(DownloadJob).where(DownloadJob.id.in_(job_ids)))).scalars()}
            rows = (await db.execute(select(WorkSource, Asset, MediaDerivativeOutbox)
                .join(AssetSource, AssetSource.work_source_id == WorkSource.id)
                .join(Asset, Asset.id == AssetSource.asset_id)
                .outerjoin(MediaDerivativeOutbox, MediaDerivativeOutbox.asset_id == Asset.id)
                .where(WorkSource.source == "manual", WorkSource.source_work_id.in_(identities)))).all()
            from app.services.search import _client, WORKS_INDEX
            work_ids = {str(w.work_id) for w, a, m in rows}
            def visible():
                index = _client().index(WORKS_INDEX)
                result = set()
                for identity in work_ids:
                    try:
                        document = index.get_document(identity)
                        if str(document["id"]) == identity:
                            result.add(identity)
                    except Exception as exc:
                        if "document_not_found" not in str(exc) and "index_not_found" not in str(exc):
                            raise
                return result
            indexed = await asyncio.to_thread(visible) if work_ids else set()
            results = []
            for item in request["items"]:
                job = jobs.get(item["job_id"])
                matched = [(w, a, m) for w, a, m in rows if w.source_work_id == item["work_source_id"]]
                events = ((job.manifest or {}).get("events", []) if job else [])
                results.append({"work_source_id": item["work_source_id"], "status": job.status if job else None,
                    "import_completed_at": next((e["at"] for e in reversed(events) if e.get("event") == "import_complete"), None),
                    "indexed": bool(matched) and all(str(w.work_id) in indexed for w, a, m in matched),
                    "assets": [{"work_id": str(w.work_id), "asset_id": str(a.id), "mime": a.mime_type,
                        "thumb": bool(a.thumb_sm_path), "sha256": bool(a.sha256), "phash": bool(a.phash),
                        "state": m.state if m else None, "completed_at": stamp(m, "completed_at") if m else None}
                        for w, a, m in matched]})
            return {"items": results}
        if request["action"] == "job":
            job_id = UUID(request["job_id"])
            job = await db.get(DownloadJob, job_id)
            imports = (await db.execute(select(ImportJob).where(ImportJob.download_job_id == job_id))).scalars().all()
            compacted_proofs = []
            if job is None and not request.get("work_source_id"):
                compacted_proofs = (await db.execute(select(StorageArtifact).where(
                    StorageArtifact.storage_root == "downloads",
                    StorageArtifact.artifact_type == "metadata_json",
                    StorageArtifact.state == "done",
                    StorageArtifact.metadata_completion_proof["download_job_id"].astext == str(job_id),
                ))).scalars().all()
                compacted_proofs = [a for a in compacted_proofs if
                    (a.metadata_completion_proof or {}).get("version") == 1 and
                    a.metadata_completion_proof.get("download_status") == "complete" and
                    a.metadata_completion_proof.get("import_status") == "complete"]
            work_source_id = request.get("work_source_id")
            if work_source_id:
                predicate = (WorkSource.source == "manual") & (WorkSource.source_work_id == work_source_id)
            else:
                artifacts = (await db.execute(select(StorageArtifact).where(StorageArtifact.download_job_id == job_id))).scalars().all()
                if compacted_proofs:
                    identities = {a.source_work_id for a in compacted_proofs}
                    source = compacted_proofs[0].source
                    artifacts = (await db.execute(select(StorageArtifact).where(
                        StorageArtifact.storage_root == "downloads", StorageArtifact.source == source,
                        StorageArtifact.source_work_id.in_(identities),
                    ))).scalars().all()
                ids = request.get("source_work_ids") or list({a.source_work_id for a in artifacts})
                predicate = (WorkSource.source == (request.get("source") or (job.source if job else compacted_proofs[0].source if compacted_proofs else "unknown"))) & WorkSource.source_work_id.in_(ids)
            rows = (await db.execute(select(WorkSource, Asset, MediaDerivativeOutbox)
                    .join(AssetSource, AssetSource.work_source_id == WorkSource.id).join(Asset, Asset.id == AssetSource.asset_id)
                    .outerjoin(MediaDerivativeOutbox, MediaDerivativeOutbox.asset_id == Asset.id).where(predicate))).all()
            work_ids = list({str(w.work_id) for w, a, m in rows})
            indexed = None
            if work_ids:
                from app.services.search import _client, WORKS_INDEX
                def visible():
                    index = _client().index(WORKS_INDEX)
                    for identity in work_ids:
                        try:
                            document = index.get_document(identity)
                        except Exception as exc:
                            if "document_not_found" in str(exc) or "index_not_found" in str(exc):
                                return False
                            raise
                        if str(document["id"]) != identity:
                            return False
                    return True
                indexed = await asyncio.to_thread(visible)
            files = artifacts if not work_source_id else []
            return {"status": job.status if job else "complete" if compacted_proofs else None, "indexed": indexed,
                    "compacted": bool(compacted_proofs),
                    "artifact_bytes": sum(a.file_size or 0 for a in files), "artifact_files": len(files),
                    "media_files": sum(a.artifact_type != "metadata_json" for a in files),
                    "imports": [{"id": str(i.id), "status": i.status, "metrics": (i.progress_data or {}).get("phase_timings_ms")} for i in imports],
                    "events": [{k: v for k, v in e.items() if k in {"at", "event", "stage", "ms", "import_job_id", "works", "assets", "to_status", "from_status", "returncode"}}
                               for e in ((job.manifest or {}).get("events", []) if job else [])],
                    "assets": [{"work_id": str(w.work_id), "source_work_id": w.source_work_id, "asset_id": str(a.id),
                        "mime": a.mime_type, "bytes": a.file_size, "thumb": bool(a.thumb_sm_path), "sha256": bool(a.sha256),
                        "phash": bool(a.phash), "state": m.state if m else None,
                        "created_at": stamp(m, "created_at") if m else None,
                        "completed_at": stamp(m, "completed_at") if m else None} for w, a, m in rows]}
    raise ValueError("Unknown control action")


async def run():
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                value = await main(json.loads(line))
            except Exception as exc:
                value = {"probe_error": type(exc).__name__, "message": str(exc)}
            print(json.dumps(value, default=str), flush=True)
    finally:
        await engine.dispose()


asyncio.run(run())
