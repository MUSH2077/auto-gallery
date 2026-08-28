"""Alembic/model parity for subscription-source identity uniqueness."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import pytest


PREDECESSOR_REVISION = "0d7e8f9a1b2c"
ALIGNMENT_REVISION = "a6c8e0f2b4d7"
OLD_CONSTRAINT = "uq_subscription_sources_sub_source"
NEW_CONSTRAINT = "uq_subscription_sources_sub_url"
OWNERSHIP_CONSTRAINT = "uq_subscription_sources_id_subscription"


def _database_url(test_database_url: str, marker: str) -> tuple[str, str, str]:
    name = f"autogallery_source_identity_{marker}_{uuid4().hex[:8]}"
    sqlalchemy_url = urlunparse(urlparse(test_database_url)._replace(path=f"/{name}"))
    asyncpg_url = sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://")
    return name, sqlalchemy_url, asyncpg_url


def _alembic(database_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    backend_dir = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=backend_dir,
        env={
            **os.environ,
            "DATABASE_URL": database_url,
            "APP_CONFIG_ROOT": "/tmp/auto-gallery-source-identity-config",
            "SECRET_KEY": "source-identity-test-secret",
            "ADMIN_PASSWORD": "source-identity-test-password",
            "REDIS_URL": "redis://:dummy@localhost:6379/0",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


async def _create_database(test_db_base_url: str, database_name: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(test_db_base_url, timeout=3)
    try:
        await connection.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await connection.close()


async def _drop_database(test_db_base_url: str, database_name: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(test_db_base_url, timeout=3)
    try:
        await connection.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
    finally:
        await connection.close()


async def _constraints(asyncpg_url: str) -> dict[str, tuple[str, ...]]:
    import asyncpg

    connection = await asyncpg.connect(asyncpg_url, timeout=3)
    try:
        return {
            row["conname"]: tuple(row["columns"])
            for row in await connection.fetch(
                """
                SELECT constraint_row.conname,
                       array_agg(attribute.attname ORDER BY key_column.ordinality) AS columns
                FROM pg_constraint AS constraint_row
                CROSS JOIN LATERAL unnest(constraint_row.conkey)
                  WITH ORDINALITY AS key_column(attnum, ordinality)
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = constraint_row.conrelid
                 AND attribute.attnum = key_column.attnum
                WHERE constraint_row.conrelid = 'subscription_sources'::regclass
                  AND constraint_row.conname = ANY($1::text[])
                GROUP BY constraint_row.conname
                """,
                [OLD_CONSTRAINT, NEW_CONSTRAINT, OWNERSHIP_CONSTRAINT],
            )
        }
    finally:
        await connection.close()


async def _seed_subscription(asyncpg_url: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(asyncpg_url, timeout=3)
    try:
        await connection.execute(
            """
            INSERT INTO creators (id, name) VALUES
              ('30000000-0000-0000-0000-000000000001', 'identity-migration')
            """
        )
        await connection.execute(
            """
            INSERT INTO subscriptions (id, creator_id, name) VALUES (
              '30000000-0000-0000-0000-000000000002',
              '30000000-0000-0000-0000-000000000001',
              'identity-migration'
            )
            """
        )
    finally:
        await connection.close()


def test_subscription_source_alignment_is_the_only_head_and_matches_model():
    """The reviewed model constraint must be represented by the single DB head."""

    import app.models  # noqa: F401 - register metadata
    from app.models import Base

    backend_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{ALIGNMENT_REVISION} (head)"

    table = Base.metadata.tables["subscription_sources"]
    model_uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.name in {NEW_CONSTRAINT, OWNERSHIP_CONSTRAINT}
    }
    assert model_uniques == {
        NEW_CONSTRAINT: ("subscription_id", "source_url"),
        OWNERSHIP_CONSTRAINT: ("id", "subscription_id"),
    }


@pytest.mark.integration
def test_upgrade_refuses_duplicate_urls_then_allows_distinct_and_null_identities(
    test_database_url,
    test_db_base_url,
):
    """Upgrade fails without mutation, then enforces URL identity semantics."""

    import asyncpg

    database_name, database_url, asyncpg_url = _database_url(
        test_database_url, "predecessor"
    )

    async def seed_duplicate_urls() -> None:
        connection = await asyncpg.connect(asyncpg_url, timeout=3)
        try:
            await connection.execute(
                """
                INSERT INTO subscription_sources (
                  id, subscription_id, source, source_creator_id, source_url
                ) VALUES
                  (
                    '30000000-0000-0000-0000-000000000011',
                    '30000000-0000-0000-0000-000000000002',
                    'pixiv', '11', 'https://identity.example/same'
                  ),
                  (
                    '30000000-0000-0000-0000-000000000012',
                    '30000000-0000-0000-0000-000000000002',
                    'x', '12', 'https://identity.example/same'
                  )
                """
            )
        finally:
            await connection.close()

    async def remove_conflict() -> None:
        connection = await asyncpg.connect(asyncpg_url, timeout=3)
        try:
            await connection.execute(
                "DELETE FROM subscription_sources "
                "WHERE id = '30000000-0000-0000-0000-000000000012'"
            )
        finally:
            await connection.close()

    async def verify_new_semantics() -> None:
        connection = await asyncpg.connect(asyncpg_url, timeout=3)
        try:
            await connection.execute(
                """
                INSERT INTO subscription_sources (
                  id, subscription_id, source, source_creator_id, source_url
                ) VALUES
                  (
                    '30000000-0000-0000-0000-000000000013',
                    '30000000-0000-0000-0000-000000000002',
                    'pixiv', '13', 'https://identity.example/distinct'
                  ),
                  (
                    '30000000-0000-0000-0000-000000000014',
                    '30000000-0000-0000-0000-000000000002',
                    'pixiv', '14', NULL
                  ),
                  (
                    '30000000-0000-0000-0000-000000000015',
                    '30000000-0000-0000-0000-000000000002',
                    'pixiv', '15', NULL
                  )
                """
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO subscription_sources (
                          id, subscription_id, source, source_creator_id, source_url
                        ) VALUES (
                          '30000000-0000-0000-0000-000000000016',
                          '30000000-0000-0000-0000-000000000002',
                          'x', '16', 'https://identity.example/distinct'
                        )
                        """
                    )
        finally:
            await connection.close()

    try:
        asyncio.run(_create_database(test_db_base_url, database_name))
        predecessor = _alembic(database_url, "upgrade", PREDECESSOR_REVISION)
        assert predecessor.returncode == 0, predecessor.stderr
        assert asyncio.run(_constraints(asyncpg_url)) == {
            OLD_CONSTRAINT: ("subscription_id", "source"),
            OWNERSHIP_CONSTRAINT: ("id", "subscription_id"),
        }
        asyncio.run(_seed_subscription(asyncpg_url))
        asyncio.run(seed_duplicate_urls())

        rejected = _alembic(database_url, "upgrade", "head")
        assert rejected.returncode != 0
        assert "duplicate non-null subscription source URLs" in rejected.stderr
        assert asyncio.run(_constraints(asyncpg_url)) == {
            OLD_CONSTRAINT: ("subscription_id", "source"),
            OWNERSHIP_CONSTRAINT: ("id", "subscription_id"),
        }

        asyncio.run(remove_conflict())
        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        repeated = _alembic(database_url, "upgrade", "head")
        assert repeated.returncode == 0, repeated.stderr
        assert asyncio.run(_constraints(asyncpg_url)) == {
            NEW_CONSTRAINT: ("subscription_id", "source_url"),
            OWNERSHIP_CONSTRAINT: ("id", "subscription_id"),
        }
        asyncio.run(verify_new_semantics())
        rejected_downgrade = _alembic(
            database_url, "downgrade", PREDECESSOR_REVISION
        )
        assert rejected_downgrade.returncode != 0
        assert (
            "duplicate subscription source providers prevent identity downgrade"
            in rejected_downgrade.stderr
        )
        assert asyncio.run(_constraints(asyncpg_url)) == {
            NEW_CONSTRAINT: ("subscription_id", "source_url"),
            OWNERSHIP_CONSTRAINT: ("id", "subscription_id"),
        }
    finally:
        asyncio.run(_drop_database(test_db_base_url, database_name))


@pytest.mark.integration
def test_fresh_upgrade_installs_model_constraint_and_repeated_upgrade_is_safe(
    test_database_url,
    test_db_base_url,
):
    """A fresh database reaches the aligned single head deterministically."""

    database_name, database_url, asyncpg_url = _database_url(test_database_url, "fresh")
    try:
        asyncio.run(_create_database(test_db_base_url, database_name))
        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        repeated = _alembic(database_url, "upgrade", "head")
        assert repeated.returncode == 0, repeated.stderr
        assert asyncio.run(_constraints(asyncpg_url)) == {
            NEW_CONSTRAINT: ("subscription_id", "source_url"),
            OWNERSHIP_CONSTRAINT: ("id", "subscription_id"),
        }
        downgraded = _alembic(database_url, "downgrade", PREDECESSOR_REVISION)
        assert downgraded.returncode == 0, downgraded.stderr
        assert asyncio.run(_constraints(asyncpg_url)) == {
            OLD_CONSTRAINT: ("subscription_id", "source"),
            OWNERSHIP_CONSTRAINT: ("id", "subscription_id"),
        }
        restored = _alembic(database_url, "upgrade", "head")
        assert restored.returncode == 0, restored.stderr
        assert asyncio.run(_constraints(asyncpg_url)) == {
            NEW_CONSTRAINT: ("subscription_id", "source_url"),
            OWNERSHIP_CONSTRAINT: ("id", "subscription_id"),
        }
    finally:
        asyncio.run(_drop_database(test_db_base_url, database_name))
