"""Prepare, submit, poll once, and version-CAS acknowledge a remote write.

No HTTP request holds a SQL transaction. Unknown submission outcomes remain
fenced: Meili lacks idempotency keys, so blindly replaying can reorder writes.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import time
from uuid import UUID, uuid4

import httpx
from sqlalchemy import and_, exists, func, or_, select, text, update

from app.config import settings
from app.database import async_session
from app.models.search_delivery_receipt import SearchDeliveryReceipt as Receipt
from app.models.search_projection_outbox import SearchProjectionOutbox as Outbox
from app.services import remote_search_flight as remote_flight

MAX_BYTES = 4 * 1024 * 1024
ACTIVE = Receipt.state.not_in(("complete", "failed"))


async def durable(function, *args):
    # Filesystem writes are not cancellable. Finish the fsync before releasing
    # local admission/writer locks, even if the scheduling deadline expires.
    operation = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await operation
        raise


def now():
    return datetime.now(timezone.utc)


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("Search delivery scheduling deadline reached")
    return value


async def set_sql_budget(db, deadline):
    budget = max(1, int(remaining(deadline) * 1000))
    await db.execute(text("SELECT set_config('statement_timeout', :value, true)"), {"value": str(budget)})
    await db.execute(text("SELECT set_config('lock_timeout', :value, true)"), {"value": str(min(1000, budget))})


@asynccontextmanager
async def session(deadline):
    async with async_session() as db:
        await set_sql_budget(db, deadline)
        yield db


def result(status, *, delay=0, claimed=0, processed=0, action=None):
    return {"status": status, "claimed": claimed, "processed": processed,
            "upserted": processed if action == "upsert" else 0,
            "deleted": processed if action == "delete" else 0,
            "failed": int(status == "error"),
            "more_likely": status not in ("idle", "ambiguous"),
            "successor_delay_seconds": delay}


async def request(client, method, path, deadline, payload=None):
    headers = {"Authorization": f"Bearer {settings.meili_master_key}"}
    kwargs = {"headers": headers, "timeout": min(5.0, remaining(deadline))}
    if payload is not None:
        kwargs["content"] = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode()
        headers["Content-Type"] = "application/json"
    response = await client.request(method, settings.meili_url.rstrip("/") + path, **kwargs)
    response.raise_for_status()
    return response.json()


async def _claim(deadline):
    token = str(uuid4())
    async with session(deadline) as db:
        row = (await db.execute(select(Receipt).where(ACTIVE).with_for_update())).scalar_one_or_none()
        if row is None:
            return None, None
        if row.available_at > now() or (row.lease_until and row.lease_until > now()):
            return row, None
        row.lease_token = token
        row.lease_until = now() + timedelta(seconds=30)
        await db.commit()
        return row, token


async def _save(receipt, token, deadline, **values):
    async with session(deadline) as db:
        changed = await db.execute(update(Receipt).where(Receipt.id == receipt.id, Receipt.lease_token == token).values(**values))
        await db.commit()
        if changed.rowcount != 1:
            raise RuntimeError("Search receipt execution lease lost")


async def _prepare(limit, deadline):
    """Global transaction lock chooses one index/action before bounded hydration."""
    from app.services.search import SearchService, WORKS_INDEX, CREATORS_INDEX, TAGS_INDEX, REPOSITORIES_INDEX, SUBSCRIPTIONS_INDEX
    builders = {WORKS_INDEX: "_build_work_documents", CREATORS_INDEX: "_build_creator_documents", TAGS_INDEX: "_build_tag_documents",
                REPOSITORIES_INDEX: "_build_repository_documents", SUBSCRIPTIONS_INDEX: "_build_subscription_documents"}
    async with session(deadline) as db:
        if not (await db.execute(text("SELECT pg_try_advisory_xact_lock(73194218)"))).scalar_one():
            return None
        if (await db.execute(select(exists().where(ACTIVE)))).scalar_one():
            return None
        ready = and_(Outbox.completed_at.is_(None), Outbox.available_at <= now(), or_(Outbox.lease_until.is_(None), Outbox.lease_until <= now()))
        head = (await db.execute(select(Outbox).where(ready).order_by(Outbox.available_at, Outbox.updated_at, Outbox.id).limit(1))).scalar_one_or_none()
        if head is None:
            return None
        rows = list((await db.execute(select(Outbox).where(ready, Outbox.index_uid == head.index_uid, Outbox.action == head.action)
                    .order_by(Outbox.available_at, Outbox.updated_at, Outbox.id).limit(max(1, min(500, limit))))).scalars())
        try:
            if head.index_uid not in builders:
                raise ValueError(f"Unsupported search outbox index {head.index_uid}")
            docs = [] if head.action == "delete" else await getattr(SearchService(db), builders[head.index_uid])([UUID(row.entity_id) for row in rows])
            by_id = {str(doc["id"]): doc for doc in docs}
            # Missing rows become deletions. Each remote receipt still contains
            # exactly one action, even when hydration exposes mixed outcomes.
            action = "upsert" if head.action == "upsert" and rows[0].entity_id in by_id else "delete"
            selected, payload = [], []
            size = 2
            for row in rows:
                actual = "upsert" if row.action == "upsert" and row.entity_id in by_id else "delete"
                if actual != action:
                    continue
                item = by_id[row.entity_id] if action == "upsert" else row.entity_id
                item_size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str).encode()) + bool(payload)
                if item_size + 2 > MAX_BYTES:
                    if not selected:
                        raise ValueError(f"Search document {row.entity_id} exceeds 4 MiB")
                    break
                if size + item_size > MAX_BYTES:
                    break
                selected.append(row)
                payload.append(item)
                size += item_size
        except Exception as exc:
            for row in rows:
                row.attempts += 1
                row.available_at = now() + timedelta(seconds=min(900, 5 * 2 ** min(row.attempts - 1, 7)))
                row.last_error = str(exc)[:4000]
            await db.commit()
            raise
        receipt = Receipt(index_uid=head.index_uid, action=action, versions=[[str(row.id), row.version] for row in selected],
                          payload=payload, state="prepared", lease_token=str(uuid4()), lease_until=now() + timedelta(seconds=30))
        db.add(receipt)
        await db.commit()
        return receipt


async def _finalize(receipt, token, deadline, *, error=None):
    async with session(deadline) as db:
        row = (await db.execute(select(Receipt).where(Receipt.id == receipt.id, Receipt.lease_token == token).with_for_update())).scalar_one()
        condition = or_(False, *(and_(Outbox.id == UUID(identity), Outbox.version == version) for identity, version in row.versions))
        if row.rebuild_id:
            from app.services.search_rebuild import complete_receipt
            await complete_receipt(db, row, error=error)
        elif row.versions:
            if error:
                await db.execute(update(Outbox).where(condition).values(attempts=Outbox.attempts + 1,
                    available_at=func.now() + func.make_interval(0, 0, 0, 0, 0, 0, func.least(900, 5 * func.power(2, func.least(Outbox.attempts, 7)))), lease_until=None, last_error=error[:4000]))
            else:
                await db.execute(update(Outbox).where(condition).values(completed_at=func.now(), lease_until=None, last_error=None))
        row.payload = []
        row.state = "failed" if error else "complete"
        row.last_error = error
        row.lease_until = None
        row.lease_token = None
        await db.commit()
    await durable(remote_flight.clear_marker, str(receipt.id))
    return result("error" if error else "ok", claimed=len(receipt.versions), processed=0 if error else len(receipt.versions), action=receipt.action)


async def _advance(receipt, token, client, deadline):
    if receipt.state in ("submitting", "ambiguous"):
        marker = await durable(remote_flight.read_marker)
        if marker and marker["owner"] == str(receipt.id) and marker.get("task_uid") is not None:
            receipt.task_uid = int(marker["task_uid"])
            receipt.state = "pending"
            await _save(receipt, token, deadline, state="pending", task_uid=receipt.task_uid)
        else:
            await _save(receipt, token, deadline, state="ambiguous", lease_until=None,
                        last_error="Submission outcome unknown; reconcile remote task identity before continuing")
            return result("ambiguous")
    if receipt.state == "prepared":
        if receipt.write_available_at and receipt.write_available_at > now():
            await _save(receipt, token, deadline, lease_until=None)
            return result("deferred", delay=(receipt.write_available_at - now()).total_seconds())
        settings_payload = None
        if receipt.phase == "settings":
            from app.services.search import INDEX_SETTINGS
            desired = INDEX_SETTINGS[receipt.index_uid]
            try:
                actual = await request(client, "GET", f"/indexes/{receipt.index_uid}/settings", deadline)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                actual = {}
            if _contains_settings(actual, desired):
                receipt.phase = "documents"
                await _save(receipt, token, deadline, phase="documents")
            else:
                settings_payload = desired
        await durable(remote_flight.create_marker, str(receipt.id), "maintenance" if receipt.action == "swap" else "search_index")
        # Durable intent MUST precede HTTP; a crash here is conservatively
        # ambiguous even if no request was actually sent.
        await durable(remote_flight.record_task, str(receipt.id), None)
        await _save(receipt, token, deadline, state="submitting")
        path = f"/indexes/{receipt.index_uid}/documents"
        method = "POST"
        if receipt.action == "delete":
            path += "/delete-batch"
        else:
            path += "?primaryKey=id"
        if receipt.action == "settings":
            method, path = "PATCH", f"/indexes/{receipt.index_uid}/settings"
        elif receipt.action == "create":
            method, path = "POST", "/indexes"
        elif receipt.action == "swap":
            method, path = "POST", "/swap-indexes"
        elif receipt.action == "drop":
            method, path = "DELETE", f"/indexes/{receipt.index_uid}"
        if settings_payload is not None:
            path = f"/indexes/{receipt.index_uid}/settings"
            method = "PATCH"
        try:
            task = await request(client, method, path, deadline, settings_payload if settings_payload is not None else receipt.payload)
            uid = int(task["taskUid"])
        except httpx.HTTPStatusError as exc:
            if receipt.action == "drop" and exc.response.status_code == 404:
                return await _finalize(receipt, token, deadline)
            if 400 <= exc.response.status_code < 500:
                return await _finalize(receipt, token, deadline, error=str(exc))
            await _save(receipt, token, deadline, state="ambiguous", lease_until=None, last_error=str(exc)[:4000])
            return result("ambiguous")
        except Exception as exc:
            await _save(receipt, token, deadline, state="ambiguous", lease_until=None, last_error=str(exc)[:4000])
            return result("ambiguous")
        await durable(remote_flight.record_task, str(receipt.id), uid)
        await _save(receipt, token, deadline, state="pending", task_uid=uid, available_at=now() + timedelta(seconds=2), lease_until=None)
        return result("pending", delay=2, claimed=len(receipt.versions))
    try:
        task = await request(client, "GET", f"/tasks/{receipt.task_uid}", deadline)
    except Exception as exc:
        await _save(receipt, token, deadline, available_at=now() + timedelta(seconds=15), lease_until=None, last_error=str(exc)[:4000])
        return result("pending", delay=15)
    if task["status"] == "succeeded":
        if receipt.phase == "settings":
            await _save(receipt, token, deadline, state="prepared", phase="documents", task_uid=None,
                        available_at=now(), lease_until=None, poll_count=0)
            await durable(remote_flight.clear_marker, str(receipt.id))
            return result("pending", delay=2)
        return await _finalize(receipt, token, deadline)
    if task["status"] in ("failed", "canceled"):
        if receipt.action == "drop" and (task.get("error") or {}).get("code") == "index_not_found":
            return await _finalize(receipt, token, deadline)
        return await _finalize(receipt, token, deadline, error=str(task.get("error") or task["status"]))
    delay = min(15, 2 * 2 ** min(receipt.poll_count + 1, 3))
    await _save(receipt, token, deadline, available_at=now() + timedelta(seconds=delay), lease_until=None, poll_count=receipt.poll_count + 1)
    return result("pending", delay=delay, claimed=len(receipt.versions))


async def run_delivery_slice(*, limit=500, client=None):
    deadline = time.monotonic() + 20
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await _run(limit, owned, deadline)
    return await _run(limit, client, deadline)


async def _run_locked(limit, client, deadline):
    # Bound SQL/connect/HTTP scheduling, with no uncancellable worker threads
    # around HTTP. Marker persistence is deliberately completed before return.
    async with asyncio.timeout(remaining(deadline)):
        receipt, token = await _claim(deadline)
        if receipt:
            if token is None:
                return result("pending", delay=max(2, (receipt.available_at - now()).total_seconds()))
            if receipt.state == "prepared":
                marker = await durable(remote_flight.read_marker)
                if marker:
                    if marker["owner"] != str(receipt.id):
                        return result("ambiguous")
                    # Prepared SQL proves this command has not been submitted.
                    # A prior settings task is already terminal, or this marker
                    # preceded the submitting commit. Re-admit every new write.
                    await durable(remote_flight.clear_marker, str(receipt.id))
                return await _admitted(limit, client, deadline, receipt, token)
            return await _advance(receipt, token, client, deadline)
        marker = await durable(remote_flight.read_marker)
        if marker:
            async with session(deadline) as db:
                previous = await db.get(Receipt, UUID(marker["owner"]))
            if previous and previous.state in ("complete", "failed"):
                await durable(remote_flight.clear_marker, marker["owner"])
            else:
                return result("ambiguous")
        return await _admitted(limit, client, deadline)


async def _admitted(limit, client, deadline, receipt=None, token=None):
    from app.services.heavy_io import adaptive_resource_slice

    async with session(deadline) as db:
        write_after = (await db.execute(select(func.max(Receipt.write_available_at)))).scalar_one_or_none()
    if write_after and write_after > now():
        if receipt:
            await _save(receipt, token, deadline, lease_until=None)
        return result("deferred", delay=(write_after - now()).total_seconds())
    from app.services import search_rebuild
    workload = "maintenance" if receipt and receipt.action == "swap" else await search_rebuild.active_workload(deadline)
    cooldown = {}
    async with adaptive_resource_slice(workload, "search-delivery", max_work_units=limit,
                                      max_slice_seconds=remaining(deadline), cooldown_result=cooldown,
                                      wait_for_capacity=False) as limits:
        if limits is None:
            if receipt:
                await _save(receipt, token, deadline, lease_until=None)
            return result("deferred", delay=2)
        local_result = None
        if receipt is None:
            try:
                next_item = await search_rebuild.prepare_next(min(limit, limits.work_units), deadline, client)
            except (ValueError, RuntimeError) as exc:
                await search_rebuild.fail_active(exc, deadline)
                next_item = result("error")
            if isinstance(next_item, dict):
                local_result = next_item
            else:
                receipt = next_item or await _prepare(min(limit, limits.work_units), deadline)
        if local_result is not None:
            outcome = local_result
        elif receipt is None:
            outcome = await _checkpoint(client, deadline)
        else:
            outcome = await _advance(receipt, token or receipt.lease_token, client, deadline)
    if cooldown.get("seconds", 0) > 0:
        if receipt:
            async with session(deadline) as db:
                await db.execute(update(Receipt).where(Receipt.id == receipt.id).values(
                    write_available_at=now() + timedelta(seconds=cooldown["seconds"]),
                ))
                await db.commit()
        else:
            outcome["successor_delay_seconds"] = max(outcome["successor_delay_seconds"], cooldown["seconds"])
    return outcome


async def _checkpoint(client, deadline):
    from app.models.repository_sync_receipt import SearchIndexState
    from app.models import Work, Creator, Tag, SubscriptionSource, Subscription
    from app.services.search import WORKS_INDEX, CREATORS_INDEX, TAGS_INDEX, REPOSITORIES_INDEX, SUBSCRIPTIONS_INDEX
    models = {WORKS_INDEX: Work, CREATORS_INDEX: Creator, TAGS_INDEX: Tag, REPOSITORIES_INDEX: SubscriptionSource, SUBSCRIPTIONS_INDEX: Subscription}
    async with session(deadline) as db:
        state = (await db.execute(select(SearchIndexState).where(
            checkpoint_due_condition(SearchIndexState),
        ).order_by(SearchIndexState.updated_at).limit(1))).scalar_one_or_none()
    if state is None or state.index_uid not in models:
        return result("idle")
    stats = await request(client, "GET", f"/indexes/{state.index_uid}/stats", deadline)
    index_count = int(stats["numberOfDocuments"])
    async with session(deadline) as db:
        count = (await db.execute(select(func.count()).select_from(models[state.index_uid]))).scalar_one()
        await db.execute(update(SearchIndexState).where(
            SearchIndexState.id == state.id,
            SearchIndexState.database_generation == state.database_generation,
            ~exists().where(Outbox.index_uid == state.index_uid, Outbox.completed_at.is_(None)),
        ).values(database_document_count=count, index_document_count=index_count,
                 indexed_generation=state.database_generation, last_verified_at=now(),
                 status="ready" if count == index_count else "drift",
                 last_error=None if count == index_count else "document_count_mismatch"))
        await db.commit()
    return result("checkpoint", delay=2)


def checkpoint_due_condition(model):
    return and_(
        model.database_generation > model.indexed_generation,
        or_(model.last_verified_at.is_(None), model.last_verified_at <= now() - timedelta(seconds=30)),
        ~exists().where(Outbox.index_uid == model.index_uid, Outbox.completed_at.is_(None)),
    )


async def _run(limit, client, deadline):
    from app.services.heavy_io import LocalHeavyIOLock, _local_lock_path

    writer = LocalHeavyIOLock(_local_lock_path().with_name("search-writer.lock"))
    if not writer.try_acquire():
        return result("busy", delay=2)
    try:
        return await _run_locked(limit, client, deadline)
    finally:
        writer.release()


_UNORDERED_STRING_LIST_PATHS = frozenset({
    ("filterableAttributes",),
    ("sortableAttributes",),
    ("nonSeparatorTokens",),
    ("typoTolerance", "disableOnAttributes"),
})


def _contains_settings(actual, desired, path=()):
    if not isinstance(actual, dict) or not isinstance(desired, dict):
        return False
    for key, desired_value in desired.items():
        if key not in actual:
            return False
        actual_value = actual[key]
        value_path = (*path, key)
        if isinstance(desired_value, dict):
            if not _contains_settings(actual_value, desired_value, value_path):
                return False
        elif value_path in _UNORDERED_STRING_LIST_PATHS:
            if (
                not isinstance(actual_value, list)
                or not isinstance(desired_value, list)
                or not all(isinstance(item, str) for item in actual_value)
                or not all(isinstance(item, str) for item in desired_value)
                or set(actual_value) != set(desired_value)
            ):
                return False
        elif actual_value != desired_value:
            return False
    return True


async def reconcile_task(receipt_id: UUID, task_uid: int, *, client=None):
    """Attach an identity explicitly verified by an operator, never a guessed match.

    The operator obtains the task UID from Meili's task history/request logs.
    A failed/canceled task then follows the normal retry path automatically.
    """
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await reconcile_task(receipt_id, task_uid, client=owned)
    from app.services.heavy_io import LocalHeavyIOLock, _local_lock_path
    writer = LocalHeavyIOLock(_local_lock_path().with_name("search-writer.lock"))
    if not writer.try_acquire():
        raise RuntimeError("Search writer is active")
    try:
        deadline = time.monotonic() + 20
        task = await request(client, "GET", f"/tasks/{int(task_uid)}", deadline)
        async with session(deadline) as db:
            receipt = (await db.execute(select(Receipt).where(Receipt.id == receipt_id).with_for_update())).scalar_one()
            if receipt.state not in ("ambiguous", "submitting"):
                raise ValueError("Receipt does not need task reconciliation")
            if receipt.lease_until and receipt.lease_until > now():
                raise ValueError("Receipt execution lease has not expired")
            expected = "settingsUpdate" if receipt.phase == "settings" or receipt.action == "settings" else {
                "delete": "documentDeletion", "upsert": "documentAdditionOrUpdate", "create": "indexCreation",
                "drop": "indexDeletion", "swap": "indexSwap",
            }[receipt.action]
            if (receipt.action != "swap" and task.get("indexUid") != receipt.index_uid) or task.get("type") != expected:
                raise ValueError("Task index/type does not match receipt")
            await durable(remote_flight.record_task, str(receipt.id), int(task_uid))
            receipt.state = "pending"
            receipt.task_uid = int(task_uid)
            receipt.available_at = now()
            receipt.lease_until = None
            receipt.last_error = None
            await db.commit()
    finally:
        writer.release()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Recover a search submission using an operator-verified Meili task identity")
    commands = parser.add_subparsers(dest="command", required=True)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--receipt", type=UUID, required=True)
    reconcile.add_argument("--task-uid", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(reconcile_task(args.receipt, args.task_uid))
    print("Task identity attached; the health coordinator will resume polling.")


if __name__ == "__main__":
    main()
