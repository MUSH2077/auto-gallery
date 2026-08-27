"""Persistence contract for private remote-follow discovery state."""

from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import JSONB


def test_private_discovery_models_register_ownership_and_source_constraints():
    """Removing user ownership or uniqueness would leak private discovery state."""
    import app.models  # noqa: F401 - registers all model tables
    from app.models import Base

    memberships = Base.metadata.tables["user_subscriptions"]
    membership_sources = Base.metadata.tables["user_subscription_sources"]
    accounts = Base.metadata.tables["remote_accounts"]
    candidates = Base.metadata.tables["discovery_candidates"]

    assert any(
        {"user_id", "subscription_id"} == set(constraint.columns.keys())
        for constraint in memberships.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    )
    assert any(
        {"user_id", "source"} == set(constraint.columns.keys())
        for constraint in accounts.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    )
    assert any(
        {"remote_account_id", "remote_creator_id"} == set(constraint.columns.keys())
        for constraint in candidates.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    )
    assert membership_sources.c.remote_account_id.nullable is True
    assert isinstance(accounts.c.scan_cursor.type, JSONB)
    assert isinstance(candidates.c.metadata.type, JSONB)
    assert all(
        foreign_key.ondelete == "RESTRICT"
        for table in (memberships, membership_sources, accounts, candidates)
        for foreign_key in table.foreign_keys
    )


def test_remote_account_input_has_safe_automatic_import_defaults_and_cap():
    """Changing opt-in/default cap could silently import an unsafe number of follows."""
    from app.schemas.remote_discovery import RemoteAccountCreate

    account = RemoteAccountCreate(source="pixiv")

    assert account.auto_import_enabled is False
    assert account.auto_import_min_confidence == "high"
    assert account.auto_import_limit == 25
    with pytest.raises(ValidationError):
        RemoteAccountCreate(source="pixiv", auto_import_limit=201)
    with pytest.raises(ValidationError):
        RemoteAccountCreate(source="mastodon")


def test_remote_account_read_and_repr_never_expose_ciphertext():
    """Adding a credential field to API reads or repr would expose a durable secret."""
    from app.models import RemoteAccount
    from app.schemas.remote_discovery import RemoteAccountRead

    account = RemoteAccount(
        id=uuid4(),
        user_id=7,
        source="x",
        credential_ciphertext="ciphertext-that-must-stay-private",
        is_enabled=True,
        scan_interval_hours=24,
        auto_import_enabled=False,
        auto_import_min_confidence="high",
        auto_import_limit=25,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    payload = RemoteAccountRead.model_validate(account).model_dump()

    assert "credential_ciphertext" not in payload
    assert "ciphertext-that-must-stay-private" not in repr(account)


def test_download_and_task_records_hold_only_optional_opaque_trigger_ids():
    """Dropping nullable trigger IDs prevents later shared download attribution."""
    from app.models import DownloadJob, TaskRun

    for model in (DownloadJob, TaskRun):
        table = model.__table__
        assert table.c.triggering_user_subscription_id.nullable is True
        assert table.c.triggering_remote_account_id.nullable is True
        assert table.c.triggering_user_subscription_id.type.python_type is type(uuid4())
        assert table.c.triggering_remote_account_id.type.python_type is type(uuid4())


def test_migration_backfills_existing_rows_to_earliest_active_administrator(monkeypatch):
    """A changed backfill query would orphan legacy subscriptions or create credentials."""
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "f4a6c8e0b2d4_add_private_remote_discovery_persistence.py"
    )
    spec = spec_from_file_location("remote_discovery_migration", migration_path)
    assert spec and spec.loader
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    statements: list[str] = []

    class Recorder:
        def create_table(self, *args, **kwargs):
            return None

        def create_index(self, *args, **kwargs):
            return None

        def add_column(self, *args, **kwargs):
            return None

        def create_foreign_key(self, *args, **kwargs):
            return None

        def execute(self, statement):
            statements.append(str(statement))

    monkeypatch.setattr(migration, "op", Recorder())
    migration.upgrade()

    backfills = [statement for statement in statements if "INSERT INTO user_subscriptions" in statement]
    assert len(backfills) == 1
    backfill = backfills[0]
    assert "is_active IS TRUE" in backfill
    assert "is_admin IS TRUE" in backfill
    assert "ORDER BY created_at ASC, id ASC" in backfill
    assert "ON CONFLICT (user_id, subscription_id) DO NOTHING" in backfill
    assert any("INSERT INTO user_subscription_sources" in statement for statement in statements)
    assert "credential_ciphertext" not in backfill
