from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def test_evidence_migration_backfills_candidate_and_account_states(monkeypatch):
    """Skipping state backfills would strand legacy rows behind new non-null gates."""
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "d0f2a4c6e8b1_add_remote_evidence_and_download_auth.py"
    )
    spec = spec_from_file_location("remote_evidence_migration", path)
    assert spec and spec.loader
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    added: list[tuple[str, str]] = []
    statements: list[str] = []

    class Recorder:
        def add_column(self, table, column):
            added.append((table, column.name))

        def create_check_constraint(self, *args, **kwargs):
            return None

        def create_index(self, *args, **kwargs):
            return None

        def alter_column(self, *args, **kwargs):
            return None

        def execute(self, statement):
            statements.append(str(statement))

    monkeypatch.setattr(migration, "op", Recorder())
    migration.upgrade()

    assert migration.down_revision == "c9e1a3b5d7f2"
    assert {
        ("discovery_candidates", "evidence_status"),
        ("discovery_candidates", "evidence_checked_at"),
        ("discovery_candidates", "evidence_error_code"),
        ("discovery_candidates", "evidence_version"),
        ("remote_accounts", "download_auth_status"),
        ("remote_accounts", "download_auth_error_reason"),
        ("remote_accounts", "last_download_auth_checked_at"),
    }.issubset(added)
    sql = "\n".join(statements)
    assert "source = 'pixiv'" in sql
    assert "state IN ('imported', 'dismissed')" in sql
    assert "missing_tweet_read_scope" in sql
    assert "download_cookie" in sql
