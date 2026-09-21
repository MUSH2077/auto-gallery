"""Alembic/model parity for manual download jobs without a source row."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import pytest


PREDECESSOR_REVISION = "a6c8e0f2b4d7"
NULLABLE_REVISION = "b8d0f2a4c6e9"
CURRENT_HEAD_REVISION = "fe46f80abc24"


def _database_url(test_database_url: str, marker: str) -> tuple[str, str, str]:
    name = f"autogallery_download_source_{marker}_{uuid4().hex[:8]}"
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
            "APP_CONFIG_ROOT": "/tmp/auto-gallery-download-source-config",
            "SECRET_KEY": "download-source-migration-test-secret",
            "ADMIN_PASSWORD": "download-source-migration-test-password",
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
        await connection.execute(
            f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)'
        )
    finally:
        await connection.close()


async def _schema_state(asyncpg_url: str) -> tuple[str, str]:
    import asyncpg

    connection = await asyncpg.connect(asyncpg_url, timeout=3)
    try:
        revision = await connection.fetchval("SELECT version_num FROM alembic_version")
        nullable = await connection.fetchval(
            """
            SELECT is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'download_jobs'
              AND column_name = 'subscription_source_id'
            """
        )
        return str(revision), str(nullable)
    finally:
        await connection.close()


async def _insert_manual_download(asyncpg_url: str, suffix: str) -> str:
    import asyncpg

    creator_id = f"40000000-0000-0000-0000-{suffix:0>12}"
    subscription_id = f"41000000-0000-0000-0000-{suffix:0>12}"
    download_id = f"42000000-0000-0000-0000-{suffix:0>12}"
    connection = await asyncpg.connect(asyncpg_url, timeout=3)
    try:
        await connection.execute(
            "INSERT INTO creators (id, name) VALUES ($1, $2)",
            creator_id,
            f"manual-source-nullable-{suffix}",
        )
        await connection.execute(
            "INSERT INTO subscriptions (id, creator_id, name) VALUES ($1, $2, $3)",
            subscription_id,
            creator_id,
            f"manual-source-nullable-{suffix}",
        )
        await connection.execute(
            """
            INSERT INTO download_jobs (
              id, subscription_id, subscription_source_id, source, source_url, status
            ) VALUES ($1, $2, NULL, 'manual', $3, 'downloaded')
            """,
            download_id,
            subscription_id,
            f"manual://{download_id}",
        )
        return download_id
    finally:
        await connection.close()


async def _delete_download(asyncpg_url: str, download_id: str) -> None:
    import asyncpg

    connection = await asyncpg.connect(asyncpg_url, timeout=3)
    try:
        await connection.execute("DELETE FROM download_jobs WHERE id = $1", download_id)
    finally:
        await connection.close()


def test_nullable_download_source_is_the_only_head_and_matches_model():
    """The single Alembic head must implement the model's nullable source FK."""

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
    assert result.stdout.strip() == f"{CURRENT_HEAD_REVISION} (head)"
    assert Base.metadata.tables["download_jobs"].c.subscription_source_id.nullable


@pytest.mark.integration
def test_predecessor_upgrade_and_fail_closed_downgrade_round_trip(
    test_database_url,
    test_db_base_url,
):
    """NULL jobs block downgrade without moving schema or revision state."""

    database_name, database_url, asyncpg_url = _database_url(
        test_database_url, "predecessor"
    )
    try:
        asyncio.run(_create_database(test_db_base_url, database_name))
        predecessor = _alembic(database_url, "upgrade", PREDECESSOR_REVISION)
        assert predecessor.returncode == 0, predecessor.stderr
        assert asyncio.run(_schema_state(asyncpg_url)) == (
            PREDECESSOR_REVISION,
            "NO",
        )

        upgraded = _alembic(database_url, "upgrade", NULLABLE_REVISION)
        assert upgraded.returncode == 0, upgraded.stderr
        assert asyncio.run(_schema_state(asyncpg_url)) == (NULLABLE_REVISION, "YES")
        download_id = asyncio.run(_insert_manual_download(asyncpg_url, "1"))

        repeated = _alembic(database_url, "upgrade", NULLABLE_REVISION)
        assert repeated.returncode == 0, repeated.stderr
        rejected = _alembic(database_url, "downgrade", PREDECESSOR_REVISION)
        assert rejected.returncode != 0
        assert "NULL download job source references prevent downgrade" in rejected.stderr
        assert "archive or explicitly remove those download_jobs rows first" in rejected.stderr
        assert asyncio.run(_schema_state(asyncpg_url)) == (NULLABLE_REVISION, "YES")

        asyncio.run(_delete_download(asyncpg_url, download_id))
        downgraded = _alembic(database_url, "downgrade", PREDECESSOR_REVISION)
        assert downgraded.returncode == 0, downgraded.stderr
        assert asyncio.run(_schema_state(asyncpg_url)) == (
            PREDECESSOR_REVISION,
            "NO",
        )
        restored = _alembic(database_url, "upgrade", NULLABLE_REVISION)
        assert restored.returncode == 0, restored.stderr
        assert asyncio.run(_schema_state(asyncpg_url)) == (NULLABLE_REVISION, "YES")
        asyncio.run(_insert_manual_download(asyncpg_url, "2"))
    finally:
        asyncio.run(_drop_database(test_db_base_url, database_name))


@pytest.mark.integration
def test_fresh_head_accepts_manual_download_without_subscription_source(
    test_database_url,
    test_db_base_url,
):
    """A fresh Alembic installation supports the production manual-upload shape."""

    database_name, database_url, asyncpg_url = _database_url(
        test_database_url, "fresh"
    )
    try:
        asyncio.run(_create_database(test_db_base_url, database_name))
        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        repeated = _alembic(database_url, "upgrade", "head")
        assert repeated.returncode == 0, repeated.stderr
        assert asyncio.run(_schema_state(asyncpg_url)) == (
            CURRENT_HEAD_REVISION,
            "YES",
        )
        asyncio.run(_insert_manual_download(asyncpg_url, "3"))
    finally:
        asyncio.run(_drop_database(test_db_base_url, database_name))
