"""Persistent consistency gates for PostgreSQL/Meilisearch hedged reads."""

from __future__ import annotations

from typing import Any

from sqlalchemy import exists, select

from app.database import async_session
from app.models.repository_sync_receipt import SearchIndexState
from app.models.search_projection_outbox import SearchProjectionOutbox
from app.services.resource_pressure import get_resource_pressure_snapshot


async def search_index_consistency(index_uid: str) -> dict[str, Any]:
    async with async_session() as db:
        state = (
            await db.execute(
                select(SearchIndexState).where(SearchIndexState.index_uid == index_uid)
            )
        ).scalar_one_or_none()
        pending = bool(
            (
                await db.execute(
                    select(
                        exists().where(
                            SearchProjectionOutbox.index_uid == index_uid,
                            SearchProjectionOutbox.completed_at.is_(None),
                        )
                    )
                )
            ).scalar_one()
        )
    pressure = await get_resource_pressure_snapshot()
    pressure_mode = str(
        pressure.get("controller_mode")
        or ("critical" if pressure.get("status") == "paused" else "constrained" if pressure.get("status") == "warning" else "normal")
    )
    generation_lag = (
        max(0, int(state.database_generation) - int(state.indexed_generation))
        if state else None
    )
    count_match = bool(
        state
        and state.database_document_count is not None
        and state.index_document_count is not None
        and state.database_document_count == state.index_document_count
    )
    generation_match = bool(
        state and state.database_generation == state.indexed_generation
    )
    consistent = bool(
        state
        and state.status == "ready"
        and generation_match
        and count_match
        and not pending
        and pressure_mode == "normal"
    )
    return {
        "consistent": consistent,
        "status": state.status if state else "unverified",
        "generation_match": generation_match,
        "count_match": count_match,
        "outbox_pending": pending,
        "generation_lag": generation_lag,
        "database_generation": int(state.database_generation) if state else None,
        "indexed_generation": int(state.indexed_generation) if state else None,
        "database_document_count": int(state.database_document_count) if state and state.database_document_count is not None else None,
        "index_document_count": int(state.index_document_count) if state and state.index_document_count is not None else None,
        "last_verified_at": state.last_verified_at.isoformat() if state and state.last_verified_at else None,
        "pressure_mode": pressure_mode,
    }
