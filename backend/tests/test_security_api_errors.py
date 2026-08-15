"""Security regression tests for administrator-facing error responses."""

import io
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError
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
async def test_subscription_batch_deletion_preserves_structured_conflict(monkeypatch):
    from app.api import subscriptions
    from app.schemas.deletion import BatchDeletionRequest
    from app.services import hierarchical_deletion

    class BlockingDeletionService:
        def __init__(self, _db):
            pass

        async def scope(self, _entity_type, _entity_ids):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "deletion_active_tasks",
                    "message": "Stop active tasks before deleting.",
                    "task_ids": [str(uuid4())],
                },
            )

    monkeypatch.setattr(
        hierarchical_deletion,
        "HierarchicalDeletionService",
        BlockingDeletionService,
    )

    with pytest.raises(HTTPException) as error:
        await subscriptions.batch_delete_subscriptions(
            BatchDeletionRequest(ids=[uuid4()]),
            Response(),
            user=SimpleNamespace(is_admin=False),
            db=object(),
        )

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "deletion_active_tasks"
    assert "private" not in str(error.value.detail)


def test_hierarchy_batch_rejects_invalid_ids_at_the_contract_boundary():
    from app.schemas.deletion import BatchDeletionRequest

    with pytest.raises(ValidationError):
        BatchDeletionRequest.model_validate({"ids": ["not-a-uuid"]})


@pytest.mark.asyncio
async def test_subscription_batch_toggle_expected_errors_use_public_codes(monkeypatch):
    from app.api import subscriptions

    class MissingSubscriptionService:
        def __init__(self, _db):
            pass

        async def update_subscription(self, _subscription_id, _data):
            raise ValueError("Subscription not found")

    monkeypatch.setattr(subscriptions, "SubscriptionService", MissingSubscriptionService)
    missing_id = str(uuid4())

    update_result = await subscriptions.batch_toggle_sync(
        {"ids": ["not-a-uuid", missing_id], "sync_enabled": False},
        db=object(),
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
