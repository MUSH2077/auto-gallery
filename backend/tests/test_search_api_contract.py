from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_search_accepts_100_and_rejects_101(monkeypatch):
    from app.api import search as search_api
    from app.database import get_db
    from app.main import app
    from app.services.search import SearchService

    async def fake_search(self, query, offset, limit, **kwargs):
        assert offset == 0
        assert limit == 100
        return {
            "canonical_query": query,
            "tokens": [],
            "groups": {"tasks": {"total": 0, "items": []}},
            "available_filters": {},
        }

    async def fake_user():
        return SimpleNamespace(is_admin=True, permissions=[], nsfw_visible=True)

    async def fake_db():
        yield None

    monkeypatch.setattr(SearchService, "search", fake_search)
    app.dependency_overrides[search_api._require_search.dependency] = fake_user
    app.dependency_overrides[get_db] = fake_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            accepted = await client.get("/api/v1/search?scope=tasks&limit=100")
            rejected = await client.get("/api/v1/search?scope=tasks&limit=101")
    finally:
        app.dependency_overrides.clear()

    assert accepted.status_code == 200, accepted.text
    assert rejected.status_code == 422
    issue = rejected.json()["detail"][0]
    assert issue["loc"][-1] == "limit"
    assert issue["type"] == "less_than_equal"
