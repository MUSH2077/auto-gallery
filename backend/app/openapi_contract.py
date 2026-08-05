from __future__ import annotations

import inspect
import re
from collections.abc import Iterable
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute, iter_route_contexts


OPENAPI_TAGS = [
    {"name": "auth", "description": "Authentication, the current account, preferences, and WebSocket tickets."},
    {"name": "system", "description": "Service health, storage, logs, scheduler state, and the operations workbench."},
    {"name": "sources", "description": "Registered content providers and their capabilities."},
    {"name": "reference", "description": "Remote reference and Danbooru-assisted import workflows."},
    {"name": "creators", "description": "Creator identities, links, source mappings, statistics, and curation."},
    {"name": "subscriptions", "description": "Subscriptions and their source repositories."},
    {"name": "repositories", "description": "Repository detail, tags, curation history, and synchronization."},
    {"name": "works", "description": "Library works, assets, tags, sources, favorites, and curation."},
    {"name": "tags", "description": "Tag browsing, editing, aliases, merging, and usage."},
    {"name": "search", "description": "Unified compound search and query assistance."},
    {"name": "download-jobs", "description": "Download queue inspection and lifecycle controls."},
    {"name": "import-jobs", "description": "Import queue inspection and lifecycle controls."},
    {"name": "tasks", "description": "Unified task history and lifecycle controls."},
    {"name": "curation", "description": "Curation commits, recovery, purge, and Gitllery operations."},
    {"name": "upload", "description": "Authenticated multipart media upload."},
    {"name": "admin", "description": "Administrative settings, backup, indexing, integrity, and deduplication operations."},
    {"name": "users", "description": "Administrator-only account and permission management."},
    {"name": "showcase", "description": "Curated showcase samples."},
]

_DEPRECATED_OPERATIONS: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/admin/dedup/duplicates"): "/api/v1/admin/dedup/cases?status=pending",
    ("POST", "/api/v1/admin/dedup/scan"): "/api/v1/admin/dedup/scans",
    ("GET", "/api/v1/admin/merge-candidates"): "/api/v1/admin/dedup/cases?status=pending",
}

_BACKGROUND_PATH_PARTS = (
    "/sync-now",
    "/rebuild",
    "/reindex",
    "/backfill",
    "/re-enrich",
    "/import-from-disk",
    "/import-all/async",
    "/dedup/scans",
    "/operations/",
)

_DANGEROUS_PATH_PARTS = (
    "/clear",
    "/cleanup",
    "/purge",
    "/restore",
    "/reset-settings",
    "/reset-password",
    "/merge",
    "/batch-delete",
    "/kill-stuck",
)


def stable_operation_id(route: APIRoute) -> str:
    method = sorted(route.methods or {"GET"})[0].lower()
    path = re.sub(r"[^a-zA-Z0-9]+", "_", route.path_format).strip("_").lower()
    return f"{method}_{path}"


def _effective_dependant(route: Any) -> Any:
    if dependant := getattr(route, "dependant", None):
        return dependant
    effective = getattr(route, "_route_context", None)
    if effective and (dependant := getattr(effective, "dependant", None)):
        return dependant
    original = getattr(route, "route", None)
    return getattr(original, "dependant", None)


def _walk_dependencies(route: Any) -> Iterable[Any]:
    dependant = _effective_dependant(route)
    pending = list(dependant.dependencies) if dependant else []
    while pending:
        dependency = pending.pop(0)
        yield dependency
        pending.extend(dependency.dependencies)


def _permission_metadata(route: Any) -> dict[str, Any]:
    permissions: set[str] = set()
    permission_mode = "allOf"
    authenticated = False
    admin_only = False

    for dependency in _walk_dependencies(route):
        call = dependency.call
        name = getattr(call, "__name__", "")
        if name in {"get_admin_key", "get_current_user", "_check", "_admin_user"}:
            authenticated = True
        if name == "_admin_user":
            admin_only = True
        try:
            nonlocals = inspect.getclosurevars(call).nonlocals
        except (TypeError, ValueError):
            nonlocals = {}
        module = nonlocals.get("module")
        if isinstance(module, str):
            permissions.add(module)
        required = nonlocals.get("required")
        if isinstance(required, tuple):
            permissions.update(value for value in required if isinstance(value, str))
            permission_mode = "anyOf"

    metadata: dict[str, Any] = {
        "x-authentication": "bearer" if authenticated else "public",
        "x-admin-only": admin_only,
    }
    if permissions:
        metadata["x-required-permissions"] = sorted(permissions)
        metadata["x-permission-mode"] = permission_mode
    return metadata


def _route_index(app: FastAPI) -> dict[tuple[str, str], Any]:
    index: dict[tuple[str, str], Any] = {}
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
        for method in route_context.methods or ():
            if method in {"HEAD", "OPTIONS"}:
                continue
            index[(method.upper(), route_context.path_format)] = route_context
    return index


def _ensure_shared_schemas(schema: dict[str, Any]) -> None:
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    schemas.setdefault(
        "ApiError",
        {
            "type": "object",
            "required": ["detail"],
            "properties": {
                "detail": {
                    "description": "Human-readable error or structured diagnostic.",
                    "anyOf": [{"type": "string"}, {"type": "object", "additionalProperties": True}],
                }
            },
        },
    )
    schemas.setdefault(
        "ValidationIssue",
        {
            "type": "object",
            "required": ["loc", "msg", "type"],
            "properties": {
                "loc": {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
                "msg": {"type": "string"},
                "type": {"type": "string"},
            },
        },
    )
    schemas.setdefault(
        "ValidationError",
        {
            "type": "object",
            "required": ["detail"],
            "properties": {
                "detail": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/ValidationIssue"},
                }
            },
        },
    )
    schemas.setdefault(
        "JsonValue",
        {
            "description": "Legacy JSON response whose narrower model has not yet been declared.",
            "anyOf": [
                {"type": "object", "additionalProperties": True},
                {"type": "array", "items": {}},
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
                {"type": "null"},
            ],
        },
    )


def _ensure_response_contract(operation: dict[str, Any]) -> None:
    responses = operation.setdefault("responses", {})
    for status_code, response in responses.items():
        if not str(status_code).startswith("2") or str(status_code) == "204":
            continue
        media_types = response.setdefault("content", {})
        if not media_types:
            media_types["application/json"] = {"schema": {"$ref": "#/components/schemas/JsonValue"}}
            operation["x-response-schema-status"] = "generic"
            continue
        for media_type, media_contract in media_types.items():
            if media_contract.get("schema"):
                continue
            media_contract["schema"] = (
                {"$ref": "#/components/schemas/JsonValue"}
                if media_type == "application/json"
                else {"type": "string", "format": "binary"}
            )
            operation["x-response-schema-status"] = "generic"


def _default_tag(path: str) -> str:
    segments = [segment for segment in path.split("/") if segment and not segment.startswith("{")]
    if "media" in segments:
        return "works"
    for tag in (
        "auth",
        "system",
        "sources",
        "reference",
        "creators",
        "subscriptions",
        "repositories",
        "works",
        "tags",
        "search",
        "download-jobs",
        "import-jobs",
        "tasks",
        "curation",
        "upload",
        "users",
        "showcase",
        "admin",
    ):
        if tag in segments:
            return tag
    return "system"


def _sanitize_public_examples(schema: dict[str, Any]) -> None:
    """Keep exported examples useful without publishing host-specific values."""

    proxy = schema.get("components", {}).get("schemas", {}).get("ProxySettings", {})
    no_proxy = proxy.get("properties", {}).get("no_proxy")
    if isinstance(no_proxy, dict):
        no_proxy.pop("default", None)
        no_proxy["example"] = "gallery.example.test"


def build_openapi_schema(app: FastAPI) -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(
        title="auto-gallery LAN API",
        version=app.version,
        description=(
            "Authenticated HTTP contract for browsing, transferring, importing, curating, and operating "
            "an auto-gallery library. Obtain a JWT with `POST /api/v1/auth/login`, then use `Bearer <token>`. "
            "Realtime task events are described by the linked AsyncAPI contract."
        ),
        routes=app.routes,
        tags=OPENAPI_TAGS,
        servers=[{"url": "/", "description": "Current auto-gallery host"}],
    )
    schema["info"]["license"] = {
        "name": "GNU Affero General Public License v3.0 only",
        "identifier": "AGPL-3.0-only",
        "url": "https://www.gnu.org/licenses/agpl-3.0.html",
    }
    schema["info"]["contact"] = {"name": "auto-gallery maintainers", "url": "https://github.com/MUSH2077/auto-gallery"}
    schema["externalDocs"] = {"description": "Realtime WebSocket contract (AsyncAPI)", "url": "/api/asyncapi.yaml"}
    _ensure_shared_schemas(schema)
    _sanitize_public_examples(schema)

    route_index = _route_index(app)
    for path, path_item in schema.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            route = route_index.get((method.upper(), path))
            if route:
                operation.update(_permission_metadata(route))
            operation.setdefault("tags", [_default_tag(path)])
            operation.setdefault("summary", operation.get("operationId", "Operation").replace("_", " ").title())
            operation.setdefault("description", "See the request, response, permission, and risk metadata for this operation.")
            _ensure_response_contract(operation)

            if operation.get("x-authentication") == "bearer":
                operation.setdefault(
                    "responses",
                    {},
                ).setdefault("401", {"description": "Missing, invalid, or expired JWT.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ApiError"}}}})
                operation["responses"].setdefault("403", {"description": "Authenticated but not permitted.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ApiError"}}}})
            operation.setdefault(
                "responses",
                {},
            ).setdefault("422", {"description": "Request or search-language validation failed.", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ValidationError"}}}})

            replacement = _DEPRECATED_OPERATIONS.get((method.upper(), path))
            if replacement:
                operation["deprecated"] = True
                operation["x-replaced-by"] = replacement
            operation["x-dangerous"] = method.upper() == "DELETE" or any(part in path for part in _DANGEROUS_PATH_PARTS)
            operation["x-background-task"] = any(part in path for part in _BACKGROUND_PATH_PARTS)

    app.openapi_schema = schema
    return schema
