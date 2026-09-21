from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_name_anchor_route_forwards_private_reference_context(monkeypatch):
    """Removing the authenticated anchor route must return 404, not an empty rail."""
    from app.api import search as search_api
    from app.database import get_db
    from app.main import app
    from app.services.search import SearchService

    captured = {}

    async def fake_name_anchors(self, *, scope, query, **kwargs):
        captured.update(scope=scope, query=query, **kwargs)
        return {
            "scope": scope,
            "direction": "asc",
            "total": 1,
            "items": [
                {
                    "key": "A",
                    "label": "A",
                    "kind": "latin",
                    "offset": 0,
                    "count": 1,
                }
            ],
        }

    async def fake_user():
        return SimpleNamespace(
            id=17,
            is_admin=False,
            permissions=["subscriptions"],
            nsfw_visible=True,
        )

    async def fake_db():
        yield None

    monkeypatch.setattr(
        SearchService,
        "name_anchors",
        fake_name_anchors,
        raising=False,
    )
    app.dependency_overrides[search_api._require_search.dependency] = fake_user
    app.dependency_overrides[get_db] = fake_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/search/name-anchors",
                params={
                    "scope": "subscriptions",
                    "q": "is:inactive sort:name-desc",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["direction"] == "asc"
    assert captured == {
        "scope": "subscriptions",
        "query": "is:inactive sort:name-desc",
        "permissions": {"subscriptions"},
        "user_id": 17,
    }


@pytest.mark.asyncio
async def test_name_anchor_route_rejects_free_text_with_stable_error_code():
    """Allowing keyword queries would expose offsets from a different ordering."""
    from app.api import search as search_api
    from app.database import get_db
    from app.main import app

    async def fake_user():
        return SimpleNamespace(
            id=17,
            is_admin=False,
            permissions=["library"],
            nsfw_visible=True,
        )

    async def fake_db():
        yield None

    app.dependency_overrides[search_api._require_search.dependency] = fake_user
    app.dependency_overrides[get_db] = fake_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/api/v1/search/name-anchors",
                params={"scope": "creators", "q": "pixiv sort:name-asc"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "name_anchors_unavailable"
