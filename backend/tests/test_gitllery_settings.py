from __future__ import annotations

import importlib
import inspect
import json
import shlex

import pytest

from gitllery_cli.main import _parser
from app.schemas.gitllery import GitllerySettingsResponse


@pytest.mark.asyncio
async def test_gitllery_settings_is_v1_shadow_read_only_and_secret_free(monkeypatch):
    endpoint = importlib.import_module("app.api.admin.settings")
    services = importlib.import_module("app.services.gitllery")

    class FakeGitlleryService:
        def __init__(self, _db):
            pass

        async def status(self, *, deep=False):
            assert deep is False
            return {
                "repositories": [],
                "missing_repos": 0,
                "behind_total": 0,
                "needs_reconcile": False,
                "product_version": "v1",
                "format_id": "gitllery-segment",
                "format_revision": 1,
                "projection_mode": "shadow",
            }

    monkeypatch.setattr(services, "GitlleryService", FakeGitlleryService)
    monkeypatch.setattr(endpoint.settings, "gitllery_projection_mode", "shadow")
    monkeypatch.setattr(endpoint.settings, "gitllery_build_generation", "segment-r1-test")

    response = GitllerySettingsResponse.model_validate(
        await endpoint.get_gitllery_settings(object())
    )
    payload = response.model_dump()

    assert payload["product_version"] == "v1"
    assert payload["format_revision"] == 1
    assert payload["projection_mode"] == "shadow"
    assert payload["managed_by"] == "deployment_environment"
    assert payload["read_only"] is True
    assert payload["governance_scope"] == {
        "observation": "host_and_auto_gallery",
        "enforcement": "auto_gallery_only",
        "modifies_other_projects": False,
        "modifies_host_configuration": False,
    }
    for name in ("automatic_projection", "reconcile", "backfill", "rebuild", "push", "pull"):
        assert payload["capabilities"][name]["enabled"] is False
    assert payload["capabilities"]["verify"]["enabled"] is True
    assert payload["capabilities"]["commit"]["enabled"] is True
    assert payload["cli"]["max_works_per_commit"] == 25
    assert payload["cli"]["max_operations_per_commit"] == 100
    assert payload["cli"]["server_stores_cli_token"] is False

    serialized = json.dumps(payload).casefold()
    for forbidden in ("password", "cookie", "secret_key", "access_token", "refresh_token", "filesystem_path"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_gitllery_settings_active_capabilities_remain_deployment_managed(monkeypatch):
    endpoint = importlib.import_module("app.api.admin.settings")
    services = importlib.import_module("app.services.gitllery")

    class FakeGitlleryService:
        def __init__(self, _db):
            pass

        async def status(self, *, deep=False):
            return {
                "repositories": [], "missing_repos": 0, "behind_total": 0,
                "product_version": "v1", "format_id": "gitllery-segment",
                "format_revision": 1, "projection_mode": "active",
            }

    monkeypatch.setattr(services, "GitlleryService", FakeGitlleryService)
    monkeypatch.setattr(endpoint.settings, "gitllery_projection_mode", "active")
    response = GitllerySettingsResponse.model_validate(
        await endpoint.get_gitllery_settings(object())
    )
    assert response.capabilities.automatic_projection.enabled is True
    assert response.capabilities.reconcile.enabled is True
    assert response.capabilities.push.enabled is False
    assert response.capabilities.push.reason == "gitllery_transfer_not_implemented"
    assert response.read_only is True


def test_gitllery_settings_cli_examples_match_the_v1_parser():
    examples = {
        "config": "gitllery config set url http://auto-gallery.test",
        "login": "gitllery auth login --username admin",
        "status": "gitllery --remote status",
        "log": "gitllery --remote log --limit 50",
        "verify": "gitllery verify --remote",
        "commit": 'gitllery --remote commit --message "curate work" work favorite 00000000-0000-0000-0000-000000000001 --set on',
    }
    parser = _parser()
    for command in examples.values():
        parsed = parser.parse_args(shlex.split(command)[1:])
        assert parsed.command in {"config", "auth", "status", "log", "verify", "commit"}


def test_gitllery_settings_route_inherits_system_permission():
    routers = importlib.import_module("app.api.admin._routers")
    assert 'APIRouter(dependencies=[RequirePermission("system")])' in inspect.getsource(routers)
