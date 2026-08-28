"""Fail-closed rollout contracts for private remote-follow discovery."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _flags(**overrides):
    values = {
        "remote_discovery_private_members_enabled": False,
        "remote_discovery_pixiv_preview_enabled": False,
        "remote_discovery_pixiv_auto_import_enabled": False,
        "remote_discovery_x_enabled": False,
        "remote_discovery_x_auto_import_enabled": False,
        "remote_discovery_bilibili_enabled": False,
        "remote_discovery_bilibili_auto_import_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _close_all(monkeypatch):
    from app.config import settings

    for name in vars(_flags()):
        monkeypatch.setattr(settings, name, False)


def test_rollout_defaults_fail_closed_and_pixiv_auto_depends_on_preview():
    """A missing foundation/preview flag must make every provider path unavailable."""
    from app.services.remote_discovery_rollout import rollout_capabilities

    closed = rollout_capabilities(_flags())
    assert closed["private_members"] is False
    assert closed["providers"]["pixiv"] == {
        "manual_preview": False,
        "auto_import": False,
        "unavailable_reason": "private_members_disabled",
    }
    assert closed["providers"]["x"]["manual_preview"] is False
    assert closed["providers"]["bilibili"]["manual_preview"] is False

    dependency_closed = rollout_capabilities(
        _flags(
            remote_discovery_private_members_enabled=True,
            remote_discovery_pixiv_auto_import_enabled=True,
        )
    )
    assert dependency_closed["providers"]["pixiv"] == {
        "manual_preview": False,
        "auto_import": False,
        "unavailable_reason": "pixiv_preview_disabled",
    }


def test_x_and_bilibili_auto_gates_are_independent_and_depend_on_preview():
    """X/Bilibili auto rollout must stay closed until each preview gate is open."""
    from app.services.remote_discovery_rollout import rollout_capabilities

    opened = rollout_capabilities(
        _flags(
            remote_discovery_private_members_enabled=True,
            remote_discovery_x_enabled=True,
            remote_discovery_bilibili_enabled=True,
        )
    )
    assert opened["providers"]["x"] == {
        "manual_preview": True,
        "auto_import": False,
        "unavailable_reason": "auto_import_not_rolled_out",
    }
    assert opened["providers"]["bilibili"] == {
        "manual_preview": True,
        "auto_import": False,
        "unavailable_reason": "auto_import_not_rolled_out",
    }

    auto_without_preview = rollout_capabilities(
        _flags(
            remote_discovery_private_members_enabled=True,
            remote_discovery_x_auto_import_enabled=True,
            remote_discovery_bilibili_auto_import_enabled=True,
        )
    )
    assert auto_without_preview["providers"]["x"]["auto_import"] is False
    assert auto_without_preview["providers"]["bilibili"]["auto_import"] is False

    fully_open = rollout_capabilities(
        _flags(
            remote_discovery_private_members_enabled=True,
            remote_discovery_x_enabled=True,
            remote_discovery_x_auto_import_enabled=True,
            remote_discovery_bilibili_enabled=True,
            remote_discovery_bilibili_auto_import_enabled=True,
        )
    )
    assert fully_open["providers"]["x"]["auto_import"] is True
    assert fully_open["providers"]["bilibili"]["auto_import"] is True


@pytest.mark.asyncio
async def test_disabled_provider_rejects_account_mutation_before_database_or_network(monkeypatch):
    """Moving rollout checks after DB/adapter use would admit disabled providers."""
    from app.services.remote_accounts import RemoteAccountService
    from app.services.remote_discovery_rollout import RemoteDiscoveryUnavailable

    class BombDatabase:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("disabled account creation reached the database")

    class UnusedVault:
        pass

    _close_all(monkeypatch)
    service = RemoteAccountService(BombDatabase(), 7, vault=UnusedVault())
    with pytest.raises(RemoteDiscoveryUnavailable) as exc_info:
        await service.create(
            {
                "source": "pixiv",
                "auth_method": "refresh_token",
                "credentials": {"refresh_token": "not-a-real-token"},
            }
        )
    assert exc_info.value.code == "private_members_disabled"


@pytest.mark.asyncio
async def test_due_admission_returns_without_querying_when_all_preview_gates_closed(monkeypatch):
    """The scheduler must not even enumerate credential rows while rollout is closed."""
    from app.services.remote_discovery import admit_due_remote_accounts

    class BombDatabase:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("disabled due admission queried private accounts")

    _close_all(monkeypatch)
    assert await admit_due_remote_accounts(BombDatabase()) == {
        "created": 0,
        "published": 0,
        "task_ids": [],
    }


@pytest.mark.asyncio
async def test_existing_x_account_cannot_auto_import_when_manual_preview_gate_is_open(monkeypatch):
    """An old auto_import_enabled row must not bypass X's manual-only rollout."""
    from app.config import settings
    from app.services.remote_discovery import RemoteDiscoveryService

    monkeypatch.setattr(settings, "remote_discovery_private_members_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_x_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_x_auto_import_enabled", False)

    class BombDatabase:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("X auto import queried candidates")

    account = SimpleNamespace(
        source="x",
        auto_import_min_confidence="high",
        auto_import_limit=25,
    )
    assert await RemoteDiscoveryService(BombDatabase()).auto_import(account) == []


@pytest.mark.asyncio
async def test_existing_accounts_remain_listable_without_loading_credential_key(monkeypatch):
    """Closing rollout or losing the key must not hide existing account metadata."""
    from app.services import remote_accounts

    class EmptyScalars:
        def all(self):
            return []

    class EmptyResult:
        def scalars(self):
            return EmptyScalars()

    class Database:
        async def execute(self, *_args, **_kwargs):
            return EmptyResult()

    def bomb_vault():
        raise AssertionError("read-only account metadata loaded REMOTE_CREDENTIAL_KEY")

    _close_all(monkeypatch)
    monkeypatch.setattr(remote_accounts, "configured_credential_vault", bomb_vault)

    service = remote_accounts.RemoteAccountService(Database(), 7)
    assert await service.list() == []


@pytest.mark.asyncio
async def test_source_api_exposes_effective_public_rollout(monkeypatch):
    """The UI must consume backend-effective flags instead of reconstructing them."""
    from app.api.sources import list_sources
    from app.config import settings

    monkeypatch.setattr(settings, "remote_discovery_private_members_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_pixiv_preview_enabled", True)
    monkeypatch.setattr(settings, "remote_discovery_pixiv_auto_import_enabled", False)
    payload = await list_sources()
    pixiv = next(item for item in payload["sources"] if item["source_name"] == "pixiv")

    assert pixiv["capabilities"]["remote_discovery_rollout"] == {
        "manual_preview": True,
        "auto_import": False,
        "unavailable_reason": "auto_import_disabled",
    }


def test_openapi_types_the_backend_effective_rollout_contract():
    """Generated clients must not receive a generic JSON rollout payload."""
    from app.main import app

    app.openapi_schema = None
    response = app.openapi()["paths"]["/api/v1/sources"]["get"]["responses"]["200"]
    schema = response["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/SourceListResponse")
    rollout = app.openapi()["components"]["schemas"]["RemoteDiscoveryRolloutRead"]
    assert set(rollout["required"]) == {
        "manual_preview",
        "auto_import",
        "unavailable_reason",
    }


def test_disabled_http_path_returns_structured_service_unavailable():
    """Clients must distinguish a rollout gate from bad credentials or input."""
    from app.api.remote_accounts import _not_found_or_bad_request
    from app.services.remote_discovery_rollout import RemoteDiscoveryUnavailable

    error = _not_found_or_bad_request(
        RemoteDiscoveryUnavailable("pixiv_preview_disabled", "pixiv")
    )
    assert error.status_code == 503
    assert error.detail == {
        "code": "remote_discovery_unavailable",
        "reason": "pixiv_preview_disabled",
        "source": "pixiv",
    }


@pytest.mark.asyncio
async def test_x_oauth_authorize_is_gated_before_state_or_remote_account_access(monkeypatch):
    """A disabled X rollout must not allocate PKCE state or touch persistence."""
    from app.api.remote_accounts import authorize_x_oauth
    from fastapi import HTTPException

    class Bomb:
        def __getattr__(self, _name):
            raise AssertionError("disabled OAuth touched a dependency")

    _close_all(monkeypatch)
    with pytest.raises(HTTPException) as exc_info:
        await authorize_x_oauth(
            account_id=None,
            db=Bomb(),
            user=SimpleNamespace(id=9),
            redis=Bomb(),
        )
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["reason"] == "private_members_disabled"


@pytest.mark.asyncio
async def test_queued_scan_rechecks_rollout_before_claim_or_adapter_call(monkeypatch):
    """Turning a gate off after enqueue must stop the worker before remote I/O."""
    from uuid import uuid4

    from app.services.remote_discovery import RemoteDiscoveryService
    from app.services.tasks import TaskService

    _close_all(monkeypatch)
    task = SimpleNamespace(id=uuid4(), status="enqueued")

    class SourceResult:
        def scalar_one_or_none(self):
            return "pixiv"

    class Database:
        def __init__(self):
            self.execute_count = 0
            self.commits = 0

        async def execute(self, *_args, **_kwargs):
            self.execute_count += 1
            if self.execute_count > 1:
                raise AssertionError("disabled worker attempted to claim the task")
            return SourceResult()

        async def get(self, *_args, **_kwargs):
            return task

        async def commit(self):
            self.commits += 1

    async def update_task(_self, target, **values):
        target.status = values["status"]
        target.reason_code = values["reason_code"]
        return target

    class BombAdapters:
        def get(self, _source):
            raise AssertionError("disabled worker reached a provider adapter")

    monkeypatch.setattr(TaskService, "update_task", update_task)
    db = Database()
    result = await RemoteDiscoveryService(db, adapters=BombAdapters()).run_scan(task.id)

    assert result.status == "failed"
    assert result.reason_code == "private_members_disabled"
    assert db.execute_count == 1
    assert db.commits == 1
