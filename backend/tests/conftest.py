"""Test fixtures for auto-gallery backend.

TEST ISOLATION (critical): the suite must NEVER touch the production database.
Previously DATABASE_URL was set with os.environ.setdefault(...), which does NOT
override the production DATABASE_URL already present in the backend/worker
containers — so destructive tests ran against the live `autogallery` DB and
wiped it. We now FORCE DATABASE_URL onto a dedicated `<db>_test` database
(reusing the real credentials, only changing the db name) before any app module
is imported, with a hard guard that it can never resolve to production.
"""

import os
from urllib.parse import urlparse, urlunparse

import pytest


def _db_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


def _with_db_name(url: str, db_name: str) -> str:
    return urlunparse(urlparse(url)._replace(path=f"/{db_name}"))


# Capture the ORIGINAL (production) URL before we override it.
_PROD_DB_URL = os.environ.get(
    # Fallback password must NOT be a factory placeholder ("changeme"/"change-me*"),
    # or config.py refuses to import (it rejects placeholder DB passwords). CI runs
    # pytest without DATABASE_URL set, so this fallback is what config validates.
    "DATABASE_URL", "postgresql+asyncpg://autogallery:test-db@postgres:5432/autogallery"
)
_PROD_DB_NAME = _db_name(_PROD_DB_URL) or "autogallery"
_MAIN_TEST_DB = _PROD_DB_NAME if _PROD_DB_NAME.endswith("_test") else f"{_PROD_DB_NAME}_test"
_MAIN_TEST_URL = os.environ.get("TEST_DATABASE_URL") or _with_db_name(_PROD_DB_URL, _MAIN_TEST_DB)

# Hard safety guard: refuse to run if the resolved test DB is the production DB.
if _db_name(_MAIN_TEST_URL) == _PROD_DB_NAME and not _PROD_DB_NAME.endswith("_test"):
    raise RuntimeError(
        f"Test isolation failure: resolved test DB '{_db_name(_MAIN_TEST_URL)}' equals the "
        f"production DB '{_PROD_DB_NAME}'. Refusing to run; set TEST_DATABASE_URL to a separate db."
    )

# OVERRIDE (not setdefault) so tests can never reach production, even when the
# container already has a production DATABASE_URL set.
os.environ["DATABASE_URL"] = _MAIN_TEST_URL
# Isolate Redis to a separate logical db so tests can't flush production keys.
_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
os.environ["REDIS_URL"] = urlunparse(urlparse(_REDIS_URL)._replace(path="/15"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("APP_CONFIG_ROOT", "/tmp/auto-gallery-test-config")
# Never let tests create or mutate the runtime gallery-dl config mount.
os.environ["GALLERYDL_CONFIG_ROOT"] = "/tmp/auto-gallery-test-gallerydl-config"


def _provision_main_test_database() -> None:
    """Create the dedicated test database (if missing) and its schema, once.

    Best-effort and self-contained (uses a throwaway engine it disposes, so the
    app's shared engine pool is not bound to this provisioning event loop). If
    PostgreSQL is unreachable, DB-less unit tests still run; DB tests fail
    naturally rather than silently falling back to production.
    """
    import asyncio

    async def _do() -> None:
        import asyncpg
        from sqlalchemy.ext.asyncio import create_async_engine
        import app.models  # noqa: F401 - registers all tables on Base.metadata
        from app.models import Base

        test_url = os.environ["DATABASE_URL"]
        db_name = _db_name(test_url)
        admin_url = _with_db_name(
            test_url.replace("postgresql+asyncpg://", "postgresql://"), "postgres"
        )
        conn = await asyncpg.connect(admin_url, timeout=3)
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", db_name
            )
            if not exists:
                await conn.execute(f'CREATE DATABASE "{db_name}"')
        finally:
            await conn.close()

        tmp_engine = create_async_engine(test_url)
        try:
            async with tmp_engine.begin() as c:
                await c.run_sync(Base.metadata.create_all)
        finally:
            await tmp_engine.dispose()

    try:
        asyncio.run(_do())
    except Exception as exc:  # noqa: BLE001 - best effort; pure tests still run
        print(f"[conftest] test DB provisioning skipped ({exc})")


_provision_main_test_database()


# ── Integration test fixtures (PostgreSQL required) ──────────────────────────

@pytest.fixture(scope="session")
def test_database_url():
    """URL for the migration-idempotency test database (created & dropped per run).

    A distinct `<db>_migration_test` database derived from the production URL, so
    it reuses the real credentials and is never the production or main-test DB.
    """
    return os.environ.get(
        "MIGRATION_TEST_DATABASE_URL",
        _with_db_name(_PROD_DB_URL, f"{_PROD_DB_NAME}_migration_test"),
    )


@pytest.fixture(scope="session")
def test_db_base_url(test_database_url):
    """Extract the connection URL for asyncpg (strips +asyncpg scheme)."""
    clean = test_database_url.replace("postgresql+asyncpg://", "postgresql://")
    parsed = urlparse(clean)
    default_db = parsed._replace(path="/postgres")
    return urlunparse(default_db)


@pytest.fixture(scope="session")
async def test_database(test_database_url, test_db_base_url):
    """Create a fresh test database, yield, then drop it.

    Uses asyncpg directly so CREATE/DROP DATABASE can run
    outside a transaction block.
    """
    import asyncpg

    parsed = urlparse(test_database_url)
    db_name = parsed.path.lstrip("/")

    try:
        conn = await asyncpg.connect(test_db_base_url, timeout=3)
    except (OSError, asyncpg.PostgresConnectionError) as exc:
        pytest.skip(f"PostgreSQL not available; skipping integration tests ({exc})")
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {db_name}")
        await conn.execute(f"CREATE DATABASE {db_name}")
    finally:
        await conn.close()

    yield test_database_url

    conn = await asyncpg.connect(test_db_base_url)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {db_name}")
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def _skip_integration_without_postgres(request, test_db_base_url):
    """Skip integration tests when PostgreSQL is not available."""
    if request.node.get_closest_marker("integration"):
        import asyncio
        import asyncpg

        try:
            async def _check():
                conn = await asyncpg.connect(test_db_base_url, timeout=3)
                await conn.close()

            asyncio.run(_check())
        except Exception:
            pytest.skip("PostgreSQL not available; skipping integration test")


@pytest.fixture
def sample_pixiv_metadata():
    return {
        "id": 38362603,
        "title": "Test Illustration",
        "description": "A test artwork",
        "date": "2024-01-01 12:00:00",
        "user": {"id": 1980643, "name": "ASK", "account": "askzy"},
        "tags": [{"name": "original"}, {"name": "test_tag"}],
        "pages": [
            {"url": "https://example.com/img_p0.jpg", "width": 800, "height": 600},
            {"url": "https://example.com/img_p1.jpg", "width": 800, "height": 600},
        ],
    }


@pytest.fixture
def sample_iwara_metadata():
    return {
        "id": "abc123",
        "title": "Test Video",
        "description": "A test iwara video",
        "type": "video",
        "rating": "ecchi",
        "tags": ["tag1", "tag2"],
        "file_id": "file_001",
        "filename": "test_video",
        "extension": "mp4",
        "width": 1920,
        "height": 1080,
        "date": "2024-06-01T12:00:00.000Z",
        "mime": "video/mp4",
        "user": {
            "id": "user123",
            "username": "test_user",
            "name": "Test User",
        },
        "url": "https://example.com/video.mp4",
    }


@pytest.fixture
def sample_twitter_metadata():
    return {
        "id_str": "1234567890123456789",
        "full_text": "Check out this artwork! #fanart",
        "created_at": "Wed Jan 01 12:00:00 +0000 2024",
        "id": 1234567890123456789,
        "user": {
            "id": 111222333,
            "name": "artist_handle",
            "nick": "Artist Name",
        },
        "entities": {
            "media": [
                {
                    "id_str": "media_001",
                    "media_url": "https://pbs.twimg.com/media/img1.jpg",
                    "media_url_https": "https://pbs.twimg.com/media/img1.jpg",
                    "sizes": {"large": {"w": 1200, "h": 800}},
                }
            ],
            "hashtags": [{"text": "fanart"}, {"text": "illustration"}],
            "user_mentions": [],
        },
        "url": "https://pbs.twimg.com/media/img1.jpg",
        "width": 1200,
        "height": 800,
    }
