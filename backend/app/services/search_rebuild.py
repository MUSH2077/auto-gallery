"""One persisted rebuild transition per scheduler slice, without sleeping workers.

Staging, keyset, replay, swap, CAS acknowledgment and cleanup are explicit
phases. A remote receipt atomically advances its cursor only after success.
"""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
import time

from sqlalchemy import and_, delete, exists, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert

from app.models.search_rebuild import SearchRebuild as Build, SearchRebuildReplay as Replay
from app.models.search_delivery_receipt import SearchDeliveryReceipt as Receipt
from app.models.search_projection_outbox import SearchProjectionOutbox as Outbox

ACTIVE_BUILD = Build.state.not_in(("complete", "failed"))


def _specs():
    from app.models import Work, Creator, Tag, SubscriptionSource, Subscription
    from app.services.search import WORKS_INDEX, CREATORS_INDEX, TAGS_INDEX, REPOSITORIES_INDEX, SUBSCRIPTIONS_INDEX
    return {WORKS_INDEX: (Work, "_build_work_documents"), CREATORS_INDEX: (Creator, "_build_creator_documents"),
            TAGS_INDEX: (Tag, "_build_tag_documents"), REPOSITORIES_INDEX: (SubscriptionSource, "_build_repository_documents"),
            SUBSCRIPTIONS_INDEX: (Subscription, "_build_subscription_documents")}


async def start_rebuild(indexes, *, batch_size=500, owner=None):
    from app.services.search_delivery import session, now
    from app.services.heavy_io import LocalHeavyIOLock, _local_lock_path
    writer = LocalHeavyIOLock(_local_lock_path().with_name("search-writer.lock"))
    if not writer.try_acquire():
        return {"status": "busy", "message": "Search writer is busy"}
    try:
        async with session(time.monotonic() + 20) as db:
            active = (await db.execute(select(Build).where(ACTIVE_BUILD))).scalar_one_or_none()
            if active:
                if owner and active.owner == str(owner):
                    return _status(active)
                raise RuntimeError("Another search rebuild is active")
            if owner:
                existing = (await db.execute(select(Build).where(Build.owner == str(owner)).order_by(Build.created_at.desc()).limit(1))).scalar_one_or_none()
                if existing:
                    return _status(existing)
            if not indexes or any(index not in _specs() for index in indexes):
                raise ValueError("Unsupported rebuild indexes")
            build = Build(id=uuid4(), owner=str(owner) if owner else None, phase="settings", progress={
                "indexes": list(dict.fromkeys(indexes)), "position": 0, "cursor": None,
                "batch_size": max(1, min(500, batch_size)), "counts": {}, "batches": 0, "replayed": 0,
                "since": now().isoformat(),
            })
            from app.services.search_projection_outbox import _mark_index_changed
            for index_uid in indexes:
                await _mark_index_changed(db, index_uid)
            db.add(build)
            await db.commit()
            return _status(build)
    finally:
        writer.release()


def _status(build):
    return {"status": "ok" if build.state == "complete" else "error" if build.state == "failed" else "pending",
            "build_id": str(build.id), "phase": build.phase, "counts": {index: build.progress.get("counts", {}).get(index, 0) for index in build.progress["indexes"]},
            "batches": build.progress.get("batches", 0), "replayed": build.progress.get("replayed", 0),
            "seconds": round(((build.updated_at if build.state in ("complete", "failed") else datetime.now(timezone.utc)) - build.created_at).total_seconds(), 1),
            "message": build.last_error or f"Search rebuild {build.phase}"}


async def rebuild_status(build_id):
    from app.services.search_delivery import session
    async with session(time.monotonic() + 20) as db:
        build = await db.get(Build, UUID(str(build_id)))
        return _status(build)


async def active_workload(deadline):
    from app.services.search_delivery import session
    async with session(deadline) as db:
        phase = (await db.execute(select(Build.phase).where(ACTIVE_BUILD))).scalar_one_or_none()
        return "maintenance" if phase == "swap" else "search_index"


def _advance_index(build, next_phase):
    progress = dict(build.progress)
    progress["position"] += 1
    progress["cursor"] = None
    if progress["position"] >= len(progress["indexes"]):
        build.phase = next_phase
        progress["position"] = 0
    build.progress = progress


def _receipt(build, uid, action, payload, progress, *, phase=None, versions=()):
    from app.services.search_delivery import now
    return Receipt(rebuild_id=build.id, index_uid=uid, action=action, payload=payload,
                   versions=list(versions), phase="raw", state="prepared", lease_token=str(uuid4()),
                   lease_until=now() + timedelta(seconds=30),
                   continuation={"phase": phase or build.phase, "progress": progress})


def _begin_failure(build, error):
    if build.state == "cleaning_failure":
        return
    build.state = "cleaning_failure"
    build.last_error = str(error)[:4000]
    # Only confirmed swap success permits live outbox acknowledgment. Otherwise
    # discard replay evidence without consuming the ordinary pending versions.
    build.phase = "acknowledge" if build.progress.get("swapped") else "discard_replay"
    build.progress = {**build.progress, "position": 0, "cursor": None}


async def prepare_next(limit, deadline, client):
    """Return None without a rebuild, a receipt, or a local phase-progress result."""
    from app.services.search_delivery import session, result, request, now, set_sql_budget
    from app.services.search import SearchService, INDEX_SETTINGS, _document_batches
    async with session(deadline) as db:
        build = (await db.execute(select(Build).where(ACTIVE_BUILD).with_for_update())).scalar_one_or_none()
        if build is None:
            return None
        # An admin cancellation prevents new remote commands; accepted commands
        # already in receipts are polled first by the delivery engine.
        if build.owner and build.state != "cleaning_failure":
            from app.models.task_run import TaskRun
            try:
                task = await db.get(TaskRun, UUID(build.owner))
            except ValueError:
                task = None
            if task and task.status in ("cancelled", "failed", "stale"):
                _begin_failure(build, "Rebuild owner stopped before the next remote command")
                await db.commit()
                return result("error")
        progress = dict(build.progress)
        indexes = progress["indexes"]
        live = indexes[progress["position"]]
        staging = f"{live}__staging_{build.id.hex[:12]}"
        receipt = None
        if build.phase == "settings":
            _advance_index(build, "build")
            receipt = _receipt(build, staging, "settings", INDEX_SETTINGS[live], build.progress, phase=build.phase)
            # The next cursor is attached to the receipt, not committed before
            # the remote settings task finishes.
            build.phase = "settings"
            build.progress = progress
        elif build.phase == "build":
            model, builder = _specs()[live]
            query = select(model.id).order_by(model.id).limit(min(limit, progress["batch_size"]))
            if progress["cursor"]:
                query = query.where(model.id > UUID(progress["cursor"]))
            identities = list((await db.execute(query)).scalars())
            if not identities:
                _advance_index(build, "replay")
            else:
                docs = await getattr(SearchService(db), builder)(identities)
                by_id = {str(doc["id"]): doc for doc in docs}
                ordered = [by_id[str(identity)] for identity in identities if str(identity) in by_id]
                payload = next(iter(_document_batches(ordered)), [])
                chosen = {doc["id"] for doc in payload}
                last = None
                for identity in identities:
                    if str(identity) in by_id and str(identity) not in chosen:
                        break
                    last = identity
                if last is None:
                    raise RuntimeError("Rebuild made no keyset progress")
                progress["cursor"] = str(last)
                progress["counts"] = {**progress["counts"], live: progress["counts"].get(live, 0) + len(payload)}
                progress["batches"] += 1
                if payload:
                    receipt = _receipt(build, staging, "upsert", payload, progress)
                else:
                    build.progress = progress
        elif build.phase == "replay":
            conditions = [Outbox.index_uid == live, or_(Outbox.completed_at.is_(None), Outbox.updated_at >= datetime.fromisoformat(progress["since"]))]
            if progress["cursor"]:
                timestamp, identity = progress["cursor"]
                conditions.append(tuple_(Outbox.updated_at, Outbox.id) > tuple_(datetime.fromisoformat(timestamp), UUID(identity)))
            events = list((await db.execute(select(Outbox).where(*conditions).order_by(Outbox.updated_at, Outbox.id).limit(min(limit, progress["batch_size"])))).scalars())
            if not events:
                _advance_index(build, "ensure_live")
            else:
                _, builder = _specs()[live]
                docs = await getattr(SearchService(db), builder)([UUID(event.entity_id) for event in events if event.action == "upsert"])
                by_id = {str(doc["id"]): doc for doc in docs}
                action = "upsert" if events[0].action == "upsert" and events[0].entity_id in by_id else "delete"
                payload, versions = [], []
                import json
                size = 2
                for event in events:
                    actual = "upsert" if event.action == "upsert" and event.entity_id in by_id else "delete"
                    if actual != action:
                        break
                    item = by_id[event.entity_id] if action == "upsert" else event.entity_id
                    length = len(json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str).encode()) + bool(payload)
                    if size + length > 4 * 1024 * 1024:
                        if not payload:
                            raise ValueError("Rebuild replay document exceeds 4 MiB")
                        break
                    payload.append(item)
                    versions.append([str(event.id), event.version])
                    progress["cursor"] = [event.updated_at.isoformat(), str(event.id)]
                    size += length
                progress["replayed"] += len(versions)
                if progress["replayed"] > 500_000:
                    raise RuntimeError("Rebuild replay exceeded 500,000 events")
                receipt = _receipt(build, staging, action, payload, progress, versions=versions)
        elif build.phase == "ensure_live":
            # End the read transaction before checking Meili. The global writer
            # flock serializes this transition against other local schedulers.
            build_id = build.id
            await db.rollback()
            try:
                await request(client, "GET", f"/indexes/{live}", deadline)
                missing = False
            except Exception as exc:
                import httpx
                if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 404:
                    raise
                missing = True
            await set_sql_budget(db, deadline)
            build = await db.get(Build, build_id)
            _advance_index(build, "swap")
            if missing:
                receipt = _receipt(build, live, "create", {"uid": live, "primaryKey": "id"}, build.progress, phase=build.phase)
                build.phase = "ensure_live"
                build.progress = progress
        elif build.phase == "swap":
            receipt = _receipt(build, "*", "swap", [{"indexes": [index, f"{index}__staging_{build.id.hex[:12]}"]} for index in indexes],
                               {**progress, "position": 0, "cursor": None, "swapped": True}, phase="acknowledge")
        elif build.phase in ("acknowledge", "discard_replay"):
            rows = list((await db.execute(select(Replay).where(Replay.build_id == build.id).limit(500))).scalars())
            if rows:
                if build.phase == "acknowledge":
                    await db.execute(update(Outbox).where(or_(*(and_(Outbox.id == row.outbox_id, Outbox.version == row.version) for row in rows)))
                                     .values(completed_at=now(), lease_until=None, last_error=None))
                await db.execute(delete(Replay).where(Replay.build_id == build.id, Replay.outbox_id.in_([row.outbox_id for row in rows])))
            else:
                build.phase = "cleanup"
        elif build.phase == "cleanup":
            _advance_index(build, "complete")
            receipt = _receipt(build, staging, "drop", {}, build.progress, phase=build.phase)
            build.phase = "cleanup"
            build.progress = progress
        if receipt:
            db.add(receipt)
        await db.commit()
        return receipt or result("pending", delay=0)


async def complete_receipt(db, receipt, *, error=None):
    build = (await db.execute(select(Build).where(Build.id == receipt.rebuild_id).with_for_update())).scalar_one()
    if error and receipt.action != "drop":
        _begin_failure(build, error)
        return
    if error:
        if build.state == "cleaning_failure":
            build.progress = {**build.progress, "cleanup_error": str(error)[:4000]}
            # Keep the failed build active until its own staging index is
            # confirmed deleted. A failed task is safe to retry after backoff.
            from app.services.search_delivery import now
            receipt.write_available_at = now() + timedelta(seconds=15)
            return
        else:
            build.last_error = f"Old index cleanup failed: {error}"
    if receipt.versions:
        statement = insert(Replay).values([
            {"build_id": build.id, "outbox_id": UUID(identity), "version": version}
            for identity, version in receipt.versions
        ])
        await db.execute(statement.on_conflict_do_update(
            index_elements=["build_id", "outbox_id"], set_={"version": statement.excluded.version},
        ))
    build.phase = receipt.continuation["phase"]
    build.progress = receipt.continuation["progress"]
    if build.phase == "complete":
        build.state = "failed" if build.state == "cleaning_failure" else "complete"
        await _finish_legacy_owner(db, build)


async def fail_active(error, deadline):
    from app.services.search_delivery import session
    async with session(deadline) as db:
        build = (await db.execute(select(Build).where(ACTIVE_BUILD).with_for_update())).scalar_one_or_none()
        if build:
            _begin_failure(build, error)
        await db.commit()


async def _finish_legacy_owner(db, build):
    if not build.owner:
        return
    from app.models.task_run import TaskRun
    from app.services.operations import ADMIN_DISPATCH_META_KEY
    from app.services.tasks import TaskService
    try:
        owner_id = UUID(build.owner)
    except ValueError:
        return
    task = (await db.execute(select(TaskRun).where(TaskRun.id == owner_id).with_for_update())).scalar_one_or_none()
    if (task is None or ADMIN_DISPATCH_META_KEY in (task.meta or {})
            or task.status not in ("enqueued", "running", "paused", "recovering")):
        return
    # The owner SELECT autoflushes the build and expires its server timestamp.
    await db.refresh(build, ["updated_at"])
    await TaskService(db).update_task(task, status=build.state,
        progress={"phase": build.state, "label": build.last_error or "Search rebuild complete"},
        result=_status(build), error=build.last_error if build.state == "failed" else None)
