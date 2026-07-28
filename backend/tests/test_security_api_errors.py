"""Security regression tests for administrator-facing error responses."""

import io
from uuid import uuid4

import pytest
from starlette.datastructures import UploadFile


@pytest.mark.asyncio
async def test_restore_backup_does_not_expose_internal_exception(monkeypatch):
    from app.api.admin import backup

    async def fail_restore(*_args, **_kwargs):
        raise RuntimeError("/private/path/database.sql")

    monkeypatch.setattr(backup.asyncio, "to_thread", fail_restore)
    upload = UploadFile(filename="backup.tar.gz", file=io.BytesIO(b"not-a-tar"))

    result = await backup.restore_backup(upload, confirm="DELETE-EVERYTHING")

    assert result["status"] == "error"
    assert result["message"] == "Restore failed. Check the backend logs for the request details."
    assert "/private/path" not in result["message"]


@pytest.mark.asyncio
async def test_gallerydl_connection_does_not_expose_internal_exception(monkeypatch):
    from app.api.admin import gallerydl
    from app.services import settings as settings_service

    def fail_config(*_args, **_kwargs):
        raise RuntimeError("credential=private-value")

    monkeypatch.setattr(settings_service, "build_effective_gallerydl_config", fail_config)

    result = await gallerydl.test_source_connection({"source": "pixiv"})

    assert result["success"] is False
    assert result["message"] == "Connection test failed unexpectedly."
    assert result["details"] == "Check the backend logs for the request details."
    assert "private-value" not in str(result)


@pytest.mark.asyncio
async def test_subscription_batch_errors_use_stable_public_code(monkeypatch):
    from app.api import subscriptions

    class FailingSubscriptionService:
        def __init__(self, _db):
            pass

        async def delete_subscription(self, _subscription_id):
            raise RuntimeError("database host is private")

    monkeypatch.setattr(subscriptions, "SubscriptionService", FailingSubscriptionService)

    result = await subscriptions.batch_delete_subscriptions(
        {"ids": [str(uuid4())]},
        db=object(),
    )

    assert result["results"][0]["error"] == "internal_error"
    assert "database host" not in str(result)


@pytest.mark.asyncio
async def test_subscription_batch_expected_errors_use_public_codes(monkeypatch):
    from app.api import subscriptions

    class MissingSubscriptionService:
        def __init__(self, _db):
            pass

        async def delete_subscription(self, _subscription_id):
            raise ValueError("Subscription not found")

        async def update_subscription(self, _subscription_id, _data):
            raise ValueError("Subscription not found")

    monkeypatch.setattr(subscriptions, "SubscriptionService", MissingSubscriptionService)
    missing_id = str(uuid4())

    result = await subscriptions.batch_delete_subscriptions(
        {"ids": ["not-a-uuid", missing_id]},
        db=object(),
    )

    assert result["results"] == [
        {"id": "not-a-uuid", "status": "error", "error": "invalid_id"},
        {"id": missing_id, "status": "error", "error": "not_found"},
    ]

    update_result = await subscriptions.batch_toggle_sync(
        {"ids": ["not-a-uuid", missing_id], "sync_enabled": False},
        db=object(),
    )

    assert update_result["results"] == [
        {"id": "not-a-uuid", "status": "error", "error": "invalid_id"},
        {"id": missing_id, "status": "error", "error": "not_found"},
    ]


@pytest.mark.asyncio
async def test_creator_batch_expected_errors_use_public_codes(monkeypatch):
    from app.api import creators

    class MissingCreatorService:
        def __init__(self, _db):
            pass

        async def delete_creator(self, _creator_id):
            raise ValueError("Creator not found")

    monkeypatch.setattr(creators, "CreatorService", MissingCreatorService)
    missing_id = str(uuid4())

    result = await creators.batch_delete_creators(
        {"ids": ["not-a-uuid", missing_id]},
        db=object(),
    )

    assert result["results"] == [
        {"id": "not-a-uuid", "status": "error", "error": "invalid_id"},
        {"id": missing_id, "status": "error", "error": "not_found"},
    ]


@pytest.mark.asyncio
async def test_creator_merge_errors_use_stable_public_code(monkeypatch):
    from app.api import creators
    from app.services import creator_dedup

    async def fail_merge(*_args, **_kwargs):
        raise RuntimeError("creator storage path is private")

    monkeypatch.setattr(creator_dedup, "merge_creators", fail_merge)

    result = await creators.merge_creators_endpoint(
        {
            "target_id": str(uuid4()),
            "source_ids": [str(uuid4())],
        },
        db=object(),
    )

    assert result["results"][0]["error"] == "merge_failed"
    assert "storage path" not in str(result)


@pytest.mark.asyncio
async def test_creator_merge_expected_errors_use_public_codes(monkeypatch):
    from app.api import creators
    from app.services import creator_dedup

    async def reject_merge(*_args, **_kwargs):
        raise ValueError("Cannot merge a creator into itself")

    monkeypatch.setattr(creator_dedup, "merge_creators", reject_merge)
    valid_source_id = str(uuid4())

    result = await creators.merge_creators_endpoint(
        {
            "target_id": str(uuid4()),
            "source_ids": ["not-a-uuid", valid_source_id],
        },
        db=object(),
    )

    assert result["results"] == [
        {"source_id": "not-a-uuid", "status": "error", "error": "invalid_id"},
        {"source_id": valid_source_id, "status": "error", "error": "merge_rejected"},
    ]
