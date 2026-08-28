"""Durable, non-secret remote credential generation contracts."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import pytest


def test_models_persist_generation_without_exposing_it_in_remote_account_api():
    """Credential identity needs durable provenance, not timestamps or API output."""

    import app.models  # noqa: F401 - register metadata
    from app.models import Base
    from app.schemas.remote_discovery import RemoteAccountRead

    accounts = Base.metadata.tables["remote_accounts"]
    jobs = Base.metadata.tables["download_jobs"]

    assert accounts.c.credential_generation.nullable is False
    assert str(accounts.c.credential_generation.server_default.arg) == "0"
    assert jobs.c.triggering_credential_generation.nullable is True
    assert "credential_generation" not in RemoteAccountRead.model_fields
    assert "triggering_credential_generation" not in RemoteAccountRead.model_fields


@pytest.mark.integration
def test_generation_migration_backfills_and_round_trips_on_isolated_postgres(
    test_database_url,
    test_db_base_url,
):
    """Upgrade assigns coherent generations and downgrade removes only new columns."""

    import asyncpg

    database_name = f"autogallery_credential_generation_{uuid4().hex[:10]}"
    database_url = urlunparse(
        urlparse(test_database_url)._replace(path=f"/{database_name}")
    )
    asyncpg_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    backend_dir = Path(__file__).resolve().parents[1]

    async def create_database() -> None:
        admin = await asyncpg.connect(test_db_base_url, timeout=3)
        try:
            await admin.execute(f'CREATE DATABASE "{database_name}"')
        finally:
            await admin.close()

    async def seed_predecessor() -> None:
        connection = await asyncpg.connect(asyncpg_url, timeout=3)
        try:
            user_id = await connection.fetchval(
                """
                INSERT INTO users (username, password_hash)
                VALUES ('credential-generation-user', 'test-only')
                RETURNING id
                """
            )
            await connection.execute(
                """
                INSERT INTO remote_accounts (
                    id, user_id, source, auth_method, credential_ciphertext
                ) VALUES
                    (
                        '10000000-0000-0000-0000-000000000001', $1,
                        'pixiv', 'refresh_token', 'encrypted-not-secret'
                    ),
                    (
                        '10000000-0000-0000-0000-000000000002', $1,
                        'x', 'cookie', NULL
                    )
                """,
                user_id,
            )
        finally:
            await connection.close()

    async def inspect_upgrade() -> tuple[list[int], tuple[str, str], set[str]]:
        connection = await asyncpg.connect(asyncpg_url, timeout=3)
        try:
            generations = list(
                await connection.fetch(
                    "SELECT credential_generation FROM remote_accounts ORDER BY id"
                )
            )
            account_column = await connection.fetchrow(
                """
                SELECT is_nullable, column_default
                FROM information_schema.columns
                WHERE table_name='remote_accounts'
                  AND column_name='credential_generation'
                """
            )
            constraints = {
                row["conname"]
                for row in await connection.fetch(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conname IN (
                        'ck_remote_accounts_credential_generation',
                        'ck_download_jobs_triggering_credential_generation'
                    )
                    """
                )
            }
            return (
                [row["credential_generation"] for row in generations],
                (account_column["is_nullable"], account_column["column_default"]),
                constraints,
            )
        finally:
            await connection.close()

    async def inspect_downgrade() -> set[tuple[str, str]]:
        connection = await asyncpg.connect(asyncpg_url, timeout=3)
        try:
            return {
                (row["table_name"], row["column_name"])
                for row in await connection.fetch(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE (table_name, column_name) IN (
                        ('remote_accounts', 'credential_generation'),
                        ('download_jobs', 'triggering_credential_generation')
                    )
                    """
                )
            }
        finally:
            await connection.close()

    async def drop_database() -> None:
        admin = await asyncpg.connect(test_db_base_url, timeout=3)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        finally:
            await admin.close()

    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "APP_CONFIG_ROOT": "/tmp/auto-gallery-credential-generation-config",
        "SECRET_KEY": "credential-generation-test-secret",
        "ADMIN_PASSWORD": "credential-generation-test-password",
        "REDIS_URL": "redis://:dummy@localhost:6379/0",
    }

    def alembic(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=backend_dir,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )

    try:
        asyncio.run(create_database())
        predecessor = alembic("upgrade", "f4a6c8e0b2d4")
        assert predecessor.returncode == 0, predecessor.stderr
        asyncio.run(seed_predecessor())

        upgraded = alembic("upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        generations, column, constraints = asyncio.run(inspect_upgrade())
        assert generations == [1, 0]
        assert column[0] == "NO"
        assert "0" in column[1]
        assert constraints == {
            "ck_remote_accounts_credential_generation",
            "ck_download_jobs_triggering_credential_generation",
        }

        downgraded = alembic("downgrade", "f4a6c8e0b2d4")
        assert downgraded.returncode == 0, downgraded.stderr
        assert asyncio.run(inspect_downgrade()) == set()
    finally:
        asyncio.run(drop_database())
