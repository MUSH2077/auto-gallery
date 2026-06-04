"""Tests for creator subscription overview API contract."""

import inspect


def test_subscription_source_read_exposes_last_synced_at():
    from app.schemas.subscription_source import SubscriptionSourceRead

    assert "last_synced_at" in SubscriptionSourceRead.model_fields


def test_creator_subscription_overview_contract():
    from app.api.creators import get_creator_subscription_overview

    src = inspect.getsource(get_creator_subscription_overview)
    assert "can_download" in src
    assert "provider.validate_url" in src
    assert "latest_job" in src
    assert "error_log_excerpt" in src
    assert "enabled_repository_count" in src
