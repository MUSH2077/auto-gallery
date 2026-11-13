"""Scheduler search and the dashboard must interpret the same source facts."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.scheduler_decisions import decision_item
from app.services.search import SearchService
from app.services.search_language import parse_search_query


def source_facts():
    source = SimpleNamespace(
        id=uuid4(), source="pixiv", source_url="https://www.pixiv.net/users/123",
        source_creator_id="123", is_enabled=True, auth_healthy=True,
        last_synced_at=None, last_attempted_at=None, next_sync_at=None,
    )
    subscription = SimpleNamespace(
        id=uuid4(), name="Artist", is_active=True, sync_enabled=True,
        schedule_mode="interval", scheduled_times=None, sync_interval_hours=6,
    )
    creator = SimpleNamespace(id=uuid4(), name="Artist", display_name=None)
    return source, subscription, creator


@pytest.mark.parametrize("auth,credentials,reason", [
    (True, False, "auth_unhealthy"),
    (False, True, "credential_missing"),
])
async def test_search_scheduler_uses_binding_blocks(monkeypatch, auth, credentials, reason):
    from app.services import settings

    config = {"scheduler_enabled": True, "schedule_mode": "interval", "timezone": "UTC"}
    monkeypatch.setattr(settings, "get_scheduler_config", AsyncMock(return_value=config))
    facts = source_facts()
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [(*facts, auth, credentials)])))
    now = datetime.now(timezone.utc)
    expected, _ = decision_item(
        *facts, auth, credentials, config=config, now=now, tz=timezone.utc,
        tz_name="UTC", scheduler_enabled=True, overdue_cutoff=now,
    )
    result = await SearchService(db)._search_scheduler(
        parse_search_query("is:blocked", "scheduler"), {}, 0, 20,
    )
    assert result["total"] == 1
    item = result["items"][0]
    assert item["due"] is False
    assert item["reason"] == expected["reason"] == reason
    assert item["auth_state"] == expected["auth_state"]
    assert item["credential_state"] == expected["credential_state"]
