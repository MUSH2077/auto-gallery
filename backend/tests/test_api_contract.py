from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml
from fastapi.routing import APIRoute, iter_route_contexts
from httpx import ASGITransport, AsyncClient


def _schema_operations(schema):
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                yield method.upper(), path, operation


def test_openapi_covers_every_http_route_with_stable_metadata():
    from app.main import app

    app.openapi_schema = None
    schema = app.openapi()
    actual = {(method, path) for method, path, _ in _schema_operations(schema)}
    expected = set()
    for route_context in iter_route_contexts(app.routes):
        if not isinstance(route_context.route, APIRoute):
            continue
        effective = getattr(route_context, "_route_context", None)
        include_in_schema = (
            getattr(effective, "include_in_schema", None)
            if effective
            else route_context.route.include_in_schema
        )
        if include_in_schema is False:
            continue
        expected.update(
            (method, route_context.path_format)
            for method in route_context.methods or ()
            if method not in {"HEAD", "OPTIONS"}
        )

    assert actual == expected
    operation_ids = []
    for method, path, operation in _schema_operations(schema):
        operation_ids.append(operation["operationId"])
        assert operation["summary"], (method, path)
        assert operation["description"], (method, path)
        assert operation["tags"], (method, path)
        assert any(str(code).startswith("2") for code in operation["responses"]), (method, path)
        assert operation["x-authentication"] in {"public", "bearer"}, (method, path)
        assert isinstance(operation["x-admin-only"], bool), (method, path)
        assert isinstance(operation["x-background-task"], bool), (method, path)
        assert isinstance(operation["x-dangerous"], bool), (method, path)

    assert len(operation_ids) == len(set(operation_ids))
    assert schema["servers"] == [{"url": "/", "description": "Current auto-gallery host"}]
    assert schema["externalDocs"]["url"] == "/api/asyncapi.yaml"
    serialized = json.dumps(schema)
    assert "127.0.0.1" not in serialized
    assert "/home/" not in serialized


def test_contract_describes_search_enums_and_pagination_limit():
    from app.main import app

    app.openapi_schema = None
    operation = app.openapi()["paths"]["/api/v1/search"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    assert parameters["limit"]["schema"]["maximum"] == 100
    assert set(parameters["scope"]["schema"]["enum"]) >= {"global", "tasks", "scheduler"}
    validation_schema = operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"]
    assert validation_schema.endswith(("/HTTPValidationError", "/ValidationError"))


def test_contract_uses_real_upload_and_binary_content_types():
    from app.main import app

    app.openapi_schema = None
    schema = app.openapi()
    upload = schema["paths"]["/api/v1/upload"]["post"]
    assert set(upload["requestBody"]["content"]) == {"multipart/form-data"}

    thumbnail = schema["paths"]["/media/thumb/{asset_id}"]["get"]["responses"]["200"]["content"]
    original = schema["paths"]["/media/original/{asset_id}"]["get"]["responses"]["200"]["content"]
    stream = schema["paths"]["/media/stream/{asset_id}"]["get"]["responses"]["206"]["content"]
    backup = schema["paths"]["/api/v1/admin/backup/download"]["get"]["responses"]["200"]["content"]
    assert set(thumbnail) == {"image/webp"}
    assert {"image/*", "video/*", "application/octet-stream"} <= set(original)
    assert {"video/mp4", "video/webm", "application/octet-stream"} <= set(stream)
    assert "application/json" not in original
    assert "application/gzip" in backup
    assert backup["application/gzip"]["schema"]["format"] == "binary"


def test_asyncapi_only_exposes_public_websocket_protocol():
    from app.api_docs import _ASYNCAPI_PATH

    schema = yaml.safe_load(_ASYNCAPI_PATH.read_text())
    assert schema["asyncapi"] == "2.6.0"
    assert set(schema["channels"]) == {"/api/v1/ws"}
    rendered = _ASYNCAPI_PATH.read_text().lower()
    assert "ping" in rendered
    assert "subscribe" in rendered
    assert "progress" in rendered


@pytest.mark.asyncio
async def test_online_contracts_require_admin_and_use_local_assets(monkeypatch):
    from app import auth
    from app.main import app

    async def fake_user(_username):
        return SimpleNamespace(is_admin=True, is_active=True)

    monkeypatch.setattr(
        auth,
        "decode_access_token_payload",
        lambda token: {"sub": "docs-admin", "pwd_chg_required": False} if token == "docs-token" else None,
    )
    monkeypatch.setattr(auth, "_load_active_user", fake_user)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        anonymous = await client.get("/api/openapi.json")
        assert anonymous.status_code == 401

        ordinary = await client.get("/api/openapi.json", headers={"Authorization": "Bearer invalid"})
        assert ordinary.status_code == 401

        cookie_schema = await client.get("/api/openapi.json", headers={"Cookie": "ag_token=docs-token"})
        assert cookie_schema.status_code == 200
        assert cookie_schema.json()["info"]["title"] == "auto-gallery LAN API"

        bearer_asyncapi = await client.get(
            "/api/asyncapi.yaml",
            headers={"Authorization": "Bearer docs-token"},
        )
        assert bearer_asyncapi.status_code == 200

        swagger = await client.get("/api/docs", headers={"Cookie": "ag_token=docs-token"})
        assert swagger.status_code == 200
        assert "/api-docs/swagger-ui-bundle.js" in swagger.text
        assert "jsdelivr" not in swagger.text

        redoc = await client.get("/api/redoc", headers={"Cookie": "ag_token=docs-token"})
        assert redoc.status_code == 200
        assert "/api-docs/redoc.standalone.js" in redoc.text
        assert "fonts.googleapis.com" not in redoc.text

        for legacy, canonical in (
            ("/docs", "/api/docs"),
            ("/redoc", "/api/redoc"),
            ("/openapi.json", "/api/openapi.json"),
        ):
            response = await client.get(legacy)
            assert response.status_code == 308
            assert response.headers["location"] == canonical


@pytest.mark.asyncio
async def test_non_admin_cannot_open_contract(monkeypatch):
    from app import auth
    from app.main import app

    async def fake_user(_username):
        return SimpleNamespace(is_admin=False, is_active=True)

    monkeypatch.setattr(
        auth,
        "decode_access_token_payload",
        lambda _token: {"sub": "ordinary-user", "pwd_chg_required": False},
    )
    monkeypatch.setattr(auth, "_load_active_user", fake_user)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/openapi.json", headers={"Cookie": "ag_token=user-token"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Administrator access required"
