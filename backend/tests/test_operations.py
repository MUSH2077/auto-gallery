import json
import asyncio
from types import SimpleNamespace
from uuid import uuid4


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def setex(self, key, ttl, value):
        self.values[key] = value.encode("utf-8")
        self.ttls[key] = ttl

    def get(self, key):
        return self.values.get(key)


def test_operation_status_roundtrip(monkeypatch):
    from app.services import operations

    fake = FakeRedis()
    monkeypatch.setattr(operations, "get_redis", lambda: fake)

    payload = operations.set_operation_status(
        "job-1",
        "running",
        "admin-clear",
        progress={"phase": "running"},
        meta={"entity": "works"},
    )

    assert payload["status"] == "running"
    assert fake.ttls[operations.operation_key("job-1")] == operations.OPERATION_TTL_SECONDS
    assert operations.get_operation_status("job-1") == json.loads(
        fake.values[operations.operation_key("job-1")].decode("utf-8")
    )


def test_unknown_operation_returns_none(monkeypatch):
    from app.services import operations

    monkeypatch.setattr(operations, "get_redis", lambda: FakeRedis())

    assert operations.get_operation_status("missing") is None


def test_invalidate_api_caches_deletes_expected_domains(monkeypatch):
    from app.services import cache

    calls = []
    def fake_delete(pattern):
        calls.append(pattern)
        return 1

    monkeypatch.setattr(cache, "cache_delete_pattern", fake_delete)

    deleted = cache.invalidate_api_caches("creators", "subscriptions", "creators")

    assert calls == ["creators:*", "subscriptions:*"]
    assert deleted == {"creators": 1, "subscriptions": 1}


def test_interactive_cache_ttls_are_shorter_than_heavy_stats():
    from app.services.cache import TTL

    assert TTL["creators:list"] == 30
    assert TTL["creators:count"] == 30
    assert TTL["subscriptions:list"] == 30
    assert TTL["creators:stats"] == 60


def test_clear_entity_data_invalidates_related_cache_domains(monkeypatch):
    from app.services import admin_data

    class Result:
        rowcount = 1

    class FakeDB:
        async def execute(self, _stmt):
            return Result()

        async def commit(self):
            pass

    calls = []
    async def fake_clear_search(_db, _entity):
        return None

    monkeypatch.setattr(admin_data, "_clear_files", lambda _paths: None)
    monkeypatch.setattr(admin_data, "_clear_search_index", fake_clear_search)
    monkeypatch.setattr(admin_data, "clear_failed_rq_jobs", lambda: 0)
    monkeypatch.setattr(
        admin_data,
        "invalidate_api_caches",
        lambda *domains: calls.append(("api", domains)),
    )
    monkeypatch.setattr(
        admin_data,
        "invalidate_creator_subscription_caches",
        lambda include_works=False: calls.append(("creator-sub", include_works)),
    )

    asyncio.run(admin_data.clear_entity_data("creators", FakeDB()))
    asyncio.run(admin_data.clear_entity_data("jobs", FakeDB()))
    asyncio.run(admin_data.clear_entity_data("all", FakeDB()))

    assert ("creator-sub", True) in calls
    assert ("api", ("subscriptions", "creators")) in calls
    assert ("api", ("tags",)) in calls


def test_danbooru_import_invalidates_creator_subscription_caches(monkeypatch):
    from app.services import danbooru_import

    class Result:
        def scalar_one_or_none(self):
            return None

    class FakeDB:
        def __init__(self):
            self.added = []
            self.committed = False

        async def get(self, _model, _id):
            return None

        def add(self, obj):
            self.added.append(obj)

        async def flush(self):
            for obj in self.added:
                if hasattr(obj, "id") and getattr(obj, "id", None) is None:
                    obj.id = uuid4()

        async def execute(self, _stmt):
            return Result()

        async def commit(self):
            self.committed = True

    calls = []
    async def fake_find_existing_creator(*_args, **_kwargs):
        return None

    async def fake_subscription_defaults(_db):
        return {"sync_interval_hours": 6, "sync_enabled": True, "is_active": True}

    monkeypatch.setattr(
        danbooru_import.danbooru_svc,
        "search_and_extract",
        lambda **_kwargs: (
            {
                "id": 123,
                "name": "artist_tag",
                "urls": [{"url": "https://www.pixiv.net/users/123", "normalized_url": "https://www.pixiv.net/users/123", "is_active": True}],
            },
            [{"url": "https://www.pixiv.net/users/123", "link_type": "profile", "source": "pixiv", "confidence": 1.0, "is_verified": True}],
        ),
    )
    monkeypatch.setattr(danbooru_import.danbooru_svc, "is_downloadable_url", lambda _url: True)
    monkeypatch.setattr(danbooru_import.danbooru_svc, "_classify_url", lambda _url: "pixiv")
    monkeypatch.setattr(danbooru_import, "find_existing_creator", fake_find_existing_creator)
    monkeypatch.setattr(danbooru_import, "get_subscription_defaults", fake_subscription_defaults)
    monkeypatch.setattr(
        danbooru_import,
        "invalidate_creator_subscription_caches",
        lambda: calls.append("invalidated"),
    )

    result = asyncio.run(danbooru_import.import_all_danbooru_artist({"pixiv_id": "123"}, FakeDB()))

    assert result["status"] == "ok"
    assert result["creator_id"]
    assert result["subscription_id"]
    assert result["links_imported"] == 1
    assert result["sources_created"] == 2
    assert calls == ["invalidated"]
