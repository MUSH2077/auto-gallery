"""Security regression tests for administrator-facing error responses."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError


@pytest.mark.asyncio
async def test_restore_backup_does_not_expose_internal_exception(monkeypatch):
    from app.api.admin import backup
    from app.services import offline_restore

    def fail_restore(*_args, **_kwargs):
        raise RuntimeError("/private/path/database.sql")

    monkeypatch.setattr(offline_restore, "create_upload_session", fail_restore)
    request = backup.RestoreUploadCreateRequest(
        filename="backup.tar.gz",
        size_bytes=1,
        sha256="0" * 64,
        chunk_size=1,
        total_chunks=1,
    )
    with pytest.raises(HTTPException) as error:
        await backup.create_restore_upload(request)

    assert error.value.status_code == 500
    assert error.value.detail == "Restore staging failed. Check the backend logs for details."
    assert "/private/path" not in error.value.detail


@pytest.mark.asyncio
async def test_gallerydl_connection_does_not_expose_internal_exception(monkeypatch):
    from app.api.admin import gallerydl
    from app.services import settings as settings_service

    def fail_config(*_args, **_kwargs):
        raise RuntimeError("credential=private-value")

    monkeypatch.setattr(settings_service, "build_effective_gallerydl_config", fail_config)

    result = await gallerydl._run_source_connection_test("pixiv")

    assert result["success"] is False
    assert result["message"] == "Connection test failed unexpectedly."
    assert result["details"] == "Check the backend logs for the request details."
    assert "private-value" not in str(result)


@pytest.mark.asyncio
async def test_subscription_batch_deletion_cannot_remove_shared_files(monkeypatch):
    from app.api import subscriptions
    from app.schemas.deletion import BatchDeletionRequest

    class BombMembershipService:
        def __init__(self, *_args):
            raise AssertionError("unauthorized file deletion touched memberships")

    monkeypatch.setattr(
        subscriptions,
        "SubscriptionMembershipService",
        BombMembershipService,
    )

    with pytest.raises(HTTPException) as error:
        await subscriptions.batch_delete_subscriptions(
            BatchDeletionRequest(ids=[uuid4()], delete_files=True),
            Response(),
            user=SimpleNamespace(id=41, is_admin=False),
            db=object(),
        )

    assert error.value.status_code == 403
    assert error.value.detail == "Administrator access required to delete files"


def test_hierarchy_batch_rejects_invalid_ids_at_the_contract_boundary():
    from app.schemas.deletion import BatchDeletionRequest

    with pytest.raises(ValidationError):
        BatchDeletionRequest.model_validate({"ids": ["not-a-uuid"]})


@pytest.mark.asyncio
async def test_subscription_batch_toggle_expected_errors_use_public_codes(monkeypatch):
    from app.api import subscriptions

    user_id = 41

    class MissingMembershipService:
        def __init__(self, _db, owner_id):
            assert owner_id == user_id

        async def update(self, _subscription_id, _data):
            raise ValueError("Subscription not found")

    class Database:
        async def commit(self):
            return None

    monkeypatch.setattr(
        subscriptions,
        "SubscriptionMembershipService",
        MissingMembershipService,
    )
    missing_id = str(uuid4())

    update_result = await subscriptions.batch_toggle_sync(
        {"ids": ["not-a-uuid", missing_id], "sync_enabled": False},
        db=Database(),
        user=SimpleNamespace(id=user_id),
    )

    assert update_result["results"] == [
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
