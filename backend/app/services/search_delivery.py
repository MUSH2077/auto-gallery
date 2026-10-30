"""Prepare and deliver bounded search commands with version-CAS acknowledgement.

PostgreSQL document upserts/deletes use a short database permit and commit the
document write, compatibility task, receipt and outbox acknowledgement together.
Settings, rebuilds, swaps and remote backends retain the existing path.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import time
from uuid import UUID, uuid4

import httpx
from sqlalchemy import and_, exists, func, or_, select, text, update, cast
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from app.models import ImportCurationOutbox
from app.services.search_index_definitions import WORKS_INDEX

from app.config import settings
from app.database import async_session
from app.models.search_delivery_receipt import SearchDeliveryReceipt as Receipt
from app.models.search_projection_outbox import SearchProjectionOutbox as Outbox
from app.services import remote_search_flight as remote_flight

MAX_BYTES = 4 * 1024 * 1024
ACTIVE = Receipt.state.not_in(("complete", "failed"))
_SHORT_HTTP_TIMEOUT_SECONDS = 5.0
_WRITE_COMPLETION_RESERVE_SECONDS = 3.0
_MIN_PREPARED_WRITE_SECONDS = 8.0
_PREPARED_BUDGET_RETRY_SECONDS = 30
_POST_SUBMIT_CONFIRM_SECONDS = 0.25
_POST_SUBMIT_POLL_INTERVAL_SECONDS = 0.05


class _WriteBudgetUnavailable(TimeoutError):
    pass


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


def _write_request_budget(deadline):
    request_budget = deadline - _WRITE_COMPLETION_RESERVE_SECONDS - time.monotonic()
    if request_budget <= 0:
        raise _WriteBudgetUnavailable("mutating search request has no completion budget")
    return request_budget


def _write_request_timeout(request_budget):
    short_budget = min(_SHORT_HTTP_TIMEOUT_SECONDS, request_budget)
    return httpx.Timeout(
        connect=short_budget,
        pool=short_budget,
        read=request_budget,
        write=request_budget,
    )


def _exception_diagnostic(exc):
    reason = str(exc).strip()
    if not reason:
        reason = "request timed out without a response" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else "no additional detail"
    return f"{type(exc).__name__}: {reason}"[:4000]


async def request(client, method, path, deadline, payload=None):
    from app.services.postgres_search_store import request as pg_request
    return await pg_request(client, method, path, deadline, payload)


async def _defer_prepared_for_budget(receipt, token, deadline, *, clear_marker=False):
    retry_at = now() + timedelta(seconds=_PREPARED_BUDGET_RETRY_SECONDS)
    error = _exception_diagnostic(_WriteBudgetUnavailable("insufficient completion budget before submission"))
    await _save(receipt, token, deadline, state="prepared", task_uid=None, available_at=retry_at,
                lease_until=None, last_error=error)
    if clear_marker:
        await durable(remote_flight.clear_marker, str(receipt.id))
    return result("deferred", delay=_PREPARED_BUDGET_RETRY_SECONDS)


async def _claim(deadline):
    token = str(uuid4())
    async with session(deadline) as db:
        row = (await db.execute(select(Receipt).where(ACTIVE).with_for_update())).scalar_one_or_none()
        if row is None:
            return None, None
        from app.services.search_backend import require_backend
        require_backend(row)
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
    from app.services.search_projection import SearchProjection, PROJECTION_SPECS
    async with session(deadline) as db:
        if not (await db.execute(text("SELECT pg_try_advisory_xact_lock(73194218)"))).scalar_one():
            return None
        if (await db.execute(select(exists().where(ACTIVE)))).scalar_one():
            return None
        base_ready = and_(Outbox.completed_at.is_(None), Outbox.available_at <= now(), or_(Outbox.lease_until.is_(None), Outbox.lease_until <= now()))
        # A work upsert is eligible only after its authoritative import curation
        # intent completes. Non-work indexes and deletes remain independently
        # drainable; this is a per-work filter, not a global barrier.
        curation_ready = or_(
            Outbox.index_uid != WORKS_INDEX,
            Outbox.action == "delete",
            ~exists().where(and_(
                ImportCurationOutbox.work_id == cast(Outbox.entity_id, PG_UUID(as_uuid=True)),
                ImportCurationOutbox.state != "complete",
            )),
        )
        ready = and_(base_ready, curation_ready)
        head = (await db.execute(select(Outbox).where(ready).order_by(Outbox.available_at, Outbox.updated_at, Outbox.id).limit(1))).scalar_one_or_none()
        if head is None:
            return None
        rows = list((await db.execute(select(Outbox).where(ready, Outbox.index_uid == head.index_uid, Outbox.action == head.action)
                    .order_by(Outbox.available_at, Outbox.updated_at, Outbox.id).limit(max(1, min(500, limit))))).scalars())
        try:
            if head.index_uid not in PROJECTION_SPECS:
                raise ValueError(f"Unsupported search outbox index {head.index_uid}")
            docs = [] if head.action == "delete" else await SearchProjection(db).build(head.index_uid, [UUID(row.entity_id) for row in rows])
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
        receipt = Receipt(backend=settings.search_backend, index_uid=head.index_uid, action=action, versions=[[str(row.id), row.version] for row in selected],
                          payload=payload, state="prepared", lease_token=str(uuid4()), lease_until=now() + timedelta(seconds=30))
        db.add(receipt)
        await db.commit()
        return receipt


async def uses_database_permit(db, receipt=None, *, lock_index=False):
    """Return whether one ordinary PG batch may use the atomic DB path."""
    if settings.search_backend != "postgresql":
        return False
    from app.services import search_rebuild
    from app.services.search_index_definitions import INDEX_SETTINGS
    from app.services.postgres_search_store import INDEX_TABLE

    if receipt is not None:
        if (
            receipt.backend != "postgresql"
            or receipt.state != "prepared"
            or receipt.rebuild_id
            or receipt.action not in ("upsert", "delete")
            or receipt.phase not in ("settings", "documents")
        ):
            return False
        index_uid = receipt.index_uid
    else:
        ready = and_(
            Outbox.completed_at.is_(None),
            Outbox.available_at <= now(),
            or_(Outbox.lease_until.is_(None), Outbox.lease_until <= now()),
            or_(
                Outbox.index_uid != WORKS_INDEX,
                Outbox.action == "delete",
                ~exists().where(and_(
                    ImportCurationOutbox.work_id == cast(Outbox.entity_id, PG_UUID(as_uuid=True)),
                    ImportCurationOutbox.state != "complete",
                )),
            ),
        )
        head = (await db.execute(
            select(Outbox.index_uid, Outbox.action)
            .where(ready)
            .order_by(Outbox.available_at, Outbox.updated_at, Outbox.id)
            .limit(1)
        )).first()
        if head is None or head.action not in ("upsert", "delete"):
            return False
        index_uid = head.index_uid

    if await db.scalar(select(exists().where(search_rebuild.ACTIVE_BUILD))):
        return False
    if await db.scalar(select(search_rebuild.membership_bootstrap_due_condition())):
        return False
    suffix = " FOR UPDATE" if lock_index else ""
    row = (await db.execute(
        text(f"SELECT settings FROM {INDEX_TABLE} WHERE uid=:uid{suffix}"),
        {"uid": index_uid},
    )).first()
    return bool(
        row
        and index_uid in INDEX_SETTINGS
        and _contains_settings(dict(row.settings or {}), INDEX_SETTINGS[index_uid])
    )


async def _apply_database_receipt(receipt, token, deadline):
    """Write documents, task history and CAS acknowledgements in one commit."""
    from app.services import postgres_search_store as store

    async with session(deadline) as db:
        row = (await db.execute(
            select(Receipt)
            .where(Receipt.id == receipt.id, Receipt.lease_token == token)
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            raise RuntimeError("Search receipt execution lease lost")
        if not await uses_database_permit(db, row, lock_index=True):
            row.lease_until = None
            await db.commit()
            return result("deferred", delay=2)
        if row.write_available_at and row.write_available_at > now():
            row.lease_until = None
            await db.commit()
            return result("deferred", delay=(row.write_available_at - now()).total_seconds())
        if row.lease_until is None or row.lease_until <= now():
            raise RuntimeError("Search receipt execution lease expired")
        if row.action == "upsert":
            task = await store._write_documents_in_session(db, row.index_uid, row.payload)
        else:
            task = await store._delete_documents_in_session(db, row.index_uid, row.payload)
        versions = list(row.versions)
        if versions:
            condition = or_(False, *(
                and_(Outbox.id == UUID(identity), Outbox.version == version)
                for identity, version in versions
            ))
            await db.execute(update(Outbox).where(condition).values(
                completed_at=func.now(), lease_until=None, last_error=None,
            ))
        row.task_uid = int(task["taskUid"])
        row.phase = "documents"
        row.payload = []
        row.state = "complete"
        row.last_error = None
        row.lease_until = None
        row.lease_token = None
        await db.flush()
        from app.services.outbox_coordinator import _curation_projection_ready, _search_ready
        successor_ready = bool(await db.scalar(select(_search_ready(now()))))
        curation_waiting = bool(await _curation_projection_ready(db))
        await db.commit()
    return {
        **result("ok", claimed=len(versions), processed=len(versions), action=receipt.action),
        "more_likely": successor_ready,
        "curation_waiting": curation_waiting,
        "database_receipt_committed": True,
    }


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


async def _confirm_new_task(client, task_uid, deadline):
    # The task UID is already durable. Give fast remote writes a short chance
    # to finish, while reserving the existing SQL/fence completion budget.
    confirm_until = min(time.monotonic() + _POST_SUBMIT_CONFIRM_SECONDS,
                        deadline - _WRITE_COMPLETION_RESERVE_SECONDS)
    if confirm_until <= time.monotonic():
        return None
    try:
        async with asyncio.timeout_at(confirm_until):
            while True:
                task = await request(client, "GET", f"/tasks/{task_uid}", confirm_until)
                if task["status"] in ("succeeded", "failed", "canceled"):
                    return task
                await asyncio.sleep(_POST_SUBMIT_POLL_INTERVAL_SECONDS)
    except Exception:
        # A read failure cannot turn a known write into an ambiguous write.
        # The durable pending receipt will use the ordinary polling path.
        return None


async def _terminal_result(receipt, token, task, deadline):
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
    return None


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
        if deadline - time.monotonic() < _MIN_PREPARED_WRITE_SECONDS:
            return await _defer_prepared_for_budget(receipt, token, deadline)
        settings_payload = None
        if receipt.phase == "settings":
            from app.services.search_index_definitions import INDEX_SETTINGS
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
        if deadline - time.monotonic() < _MIN_PREPARED_WRITE_SECONDS:
            return await _defer_prepared_for_budget(receipt, token, deadline)
        await durable(remote_flight.create_marker, str(receipt.id), "maintenance" if receipt.action == "swap" else "search_index")
        # Durable intent MUST precede HTTP; a crash here is conservatively
        # ambiguous even if no request was actually sent.
        await durable(remote_flight.record_task, str(receipt.id), None)
        await _save(receipt, token, deadline, state="submitting", last_error=None)
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
        except _WriteBudgetUnavailable:
            return await _defer_prepared_for_budget(receipt, token, deadline, clear_marker=True)
        except httpx.HTTPStatusError as exc:
            if receipt.action == "drop" and exc.response.status_code == 404:
                return await _finalize(receipt, token, deadline)
            if 400 <= exc.response.status_code < 500:
                return await _finalize(receipt, token, deadline, error=_exception_diagnostic(exc))
            await _save(receipt, token, deadline, state="ambiguous", lease_until=None, last_error=_exception_diagnostic(exc))
            return result("ambiguous")
        except Exception as exc:
            await _save(receipt, token, deadline, state="ambiguous", lease_until=None, last_error=_exception_diagnostic(exc))
            return result("ambiguous")
        await durable(remote_flight.record_task, str(receipt.id), uid)
        await _save(receipt, token, deadline, state="pending", task_uid=uid, available_at=now() + timedelta(seconds=1), lease_until=None)
        receipt.task_uid = uid
        task = await _confirm_new_task(client, uid, deadline)
        if task is not None:
            return await _terminal_result(receipt, token, task, deadline)
        return result("pending", delay=1, claimed=len(receipt.versions))
    try:
        task = await request(client, "GET", f"/tasks/{receipt.task_uid}", deadline)
    except Exception as exc:
        await _save(receipt, token, deadline, available_at=now() + timedelta(seconds=15), lease_until=None,
                    last_error=_exception_diagnostic(exc))
        return result("pending", delay=15)
    terminal = await _terminal_result(receipt, token, task, deadline)
    if terminal is not None:
        return terminal
    delay = min(15, 2 ** min(receipt.poll_count + 1, 4))
    await _save(receipt, token, deadline, available_at=now() + timedelta(seconds=delay), lease_until=None, poll_count=receipt.poll_count + 1)
    return result("pending", delay=delay, claimed=len(receipt.versions))


async def run_delivery_slice(*, limit=500, client=None):
    """Run one bounded search slice, continuing only ordinary PG receipts."""
    deadline = time.monotonic() + 20
    if settings.search_backend != "postgresql":
        return await _run(limit, client, deadline)
    budget = {"remaining_units": max(1, int(limit)), "deadline": deadline}
    aggregate = None
    outcome = None
    while budget["remaining_units"] > 0 and time.monotonic() < budget["deadline"]:
        outcome = await _run(budget["remaining_units"], client, deadline, budget=budget)
        if aggregate is None:
            aggregate = {"claimed": 0, "processed": 0, "upserted": 0, "deleted": 0, "failed": 0, "continuation_receipts": 0}
        for key in ("claimed", "processed", "upserted", "deleted", "failed"):
            aggregate[key] += int(outcome.get(key) or 0)
        committed = bool(outcome.get("database_receipt_committed"))
        used = int(outcome.get("claimed") or 0)
        aggregate["continuation_receipts"] += int(committed)
        budget["remaining_units"] = max(0, budget["remaining_units"] - max(0, used))
        if (
            not committed or outcome.get("status") != "ok"
            or not outcome.get("more_likely") or outcome.get("curation_waiting")
            or outcome.get("successor_delay_seconds", 0) > 0 or used <= 0
        ):
            break
    if outcome is None:
        return result("idle")
    result_value = dict(outcome)
    if aggregate:
        result_value.update(aggregate)
    result_value["bounded_continuation"] = bool(aggregate and aggregate["continuation_receipts"] > 1)
    return result_value


async def _run_locked(limit, client, deadline, budget=None):
    # Bound SQL/connect/HTTP scheduling, with no uncancellable worker threads
    # around HTTP. Marker persistence is deliberately completed before return.
    async with asyncio.timeout(remaining(deadline)):
        receipt, token = await _claim(deadline)
        if receipt:
            if token is None:
                return result("pending", delay=max(1, (receipt.available_at - now()).total_seconds()))
            if receipt.state == "prepared":
                marker = await durable(remote_flight.read_marker)
                if marker:
                    if marker["owner"] != str(receipt.id):
                        return result("ambiguous")
                    # Prepared SQL proves this command has not been submitted.
                    # A prior settings task is already terminal, or this marker
                    # preceded the submitting commit. Re-admit every new write.
                    await durable(remote_flight.clear_marker, str(receipt.id))
                return await _admitted(limit, client, deadline, receipt, token, budget=budget)
            return await _advance(receipt, token, client, deadline)
        marker = await durable(remote_flight.read_marker)
        if marker:
            async with session(deadline) as db:
                previous = await db.get(Receipt, UUID(marker["owner"]))
            if previous and previous.state in ("complete", "failed"):
                await durable(remote_flight.clear_marker, marker["owner"])
            else:
                return result("ambiguous")
        return await _admitted(limit, client, deadline, budget=budget)


async def _admitted(limit, client, deadline, receipt=None, token=None, *, budget=None):
    from app.services.heavy_io import adaptive_resource_slice

    async with session(deadline) as db:
        write_after = (await db.execute(select(func.max(Receipt.write_available_at)))).scalar_one_or_none()
    if write_after and write_after > now():
        if receipt:
            await _save(receipt, token, deadline, lease_until=None)
        return result("deferred", delay=(write_after - now()).total_seconds())
    from app.services import search_rebuild
    pg_permit = False
    if settings.search_backend == "postgresql":
        async with session(deadline) as db:
            pg_permit = await uses_database_permit(db, receipt)
    workload = (
        "pg_search_db"
        if pg_permit
        else "maintenance" if receipt and receipt.action == "swap"
        else await search_rebuild.active_workload(deadline)
    )
    cooldown = {}
    slice_seconds = remaining(deadline)
    if pg_permit and budget is not None:
        slice_seconds = min(slice_seconds, budget["deadline"] - time.monotonic())
        if slice_seconds <= 0:
            if receipt:
                await _save(receipt, token, deadline, lease_until=None)
            return result("pending")
    async with adaptive_resource_slice(workload, "search-delivery", max_work_units=limit,
                                      max_slice_seconds=slice_seconds, cooldown_result=cooldown,
                                      wait_for_capacity=False) as limits:
        if limits is None:
            if receipt:
                await _save(receipt, token, deadline, lease_until=None)
            if settings.search_backend == "postgresql" and workload in {"search_index", "pg_search_db"}:
                return {**result("deferred"), "resource_deferred": True, "more_likely": False}
            return result("deferred", delay=2)
        work_units = min(limit, limits.work_units)
        if pg_permit and budget is not None:
            if time.monotonic() >= budget["deadline"]:
                if receipt:
                    await _save(receipt, token, deadline, lease_until=None)
                return result("pending")
            budget["remaining_units"] = min(budget["remaining_units"], work_units)
            if limits.slice_seconds:
                budget["deadline"] = min(
                    budget["deadline"], time.monotonic() + limits.slice_seconds,
                )
            # Finish an admitted atomic receipt under the original hard deadline.
            # The shared soft deadline only stops starting another receipt.
        local_result = None
        if receipt is None:
            try:
                next_item = await search_rebuild.prepare_next(work_units, deadline, client)
            except (ValueError, RuntimeError) as exc:
                await search_rebuild.fail_active(exc, deadline)
                next_item = result("error")
            if isinstance(next_item, dict):
                local_result = next_item
            else:
                receipt = next_item or await _prepare(work_units, deadline)
        if local_result is not None:
            outcome = local_result
        elif pg_permit:
            prepared = receipt or await _prepare(work_units, deadline)
            if prepared is None:
                outcome = await _checkpoint(client, deadline)
            else:
                outcome = await _apply_database_receipt(
                    prepared, token or prepared.lease_token, deadline
                )
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
        if receipt is None or outcome.get("database_receipt_committed"):
            outcome["successor_delay_seconds"] = max(outcome["successor_delay_seconds"], cooldown["seconds"])
    return outcome


async def _checkpoint(client, deadline):
    from app.models.repository_sync_receipt import SearchIndexState
    from app.services.search_projection import PROJECTION_SPECS
    models = {index: spec.model for index, spec in PROJECTION_SPECS.items()}
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


async def _run(limit, client, deadline, budget=None):
    from app.services.heavy_io import LocalHeavyIOLock, _local_lock_path

    writer = LocalHeavyIOLock(_local_lock_path().with_name("search-writer.lock"))
    if not writer.try_acquire():
        return result("busy", delay=2)
    try:
        return await _run_locked(limit, client, deadline, budget=budget)
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
        if value_path in _UNORDERED_STRING_LIST_PATHS:
            if (
                not isinstance(actual_value, list)
                or not isinstance(desired_value, list)
                or not all(isinstance(item, str) for item in actual_value)
                or not all(isinstance(item, str) for item in desired_value)
                or set(actual_value) != set(desired_value)
            ):
                return False
        elif isinstance(desired_value, dict):
            if not _contains_settings(actual_value, desired_value, value_path):
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
