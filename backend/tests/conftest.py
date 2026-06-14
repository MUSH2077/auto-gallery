"""Test fixtures for auto-gallery backend."""

import os
from urllib.parse import urlparse, urlunparse

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://autogallery:test-db@postgres:5432/autogallery")
os.environ.setdefault("REDIS_URL", "redis://:test-redis@redis:6379/0")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("APP_CONFIG_ROOT", "/tmp/auto-gallery-test-config")


# ── Integration test fixtures (PostgreSQL required) ──────────────────────────

@pytest.fixture(scope="session")
def test_database_url():
    """Return the URL for the isolated test database.

    Defaults to the Docker Compose postgres service, but honors
    TEST_DATABASE_URL env var for CI (where the host is localhost).
    """
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://autogallery:change-me-postgres@postgres:5432/auto_gallery_migration_test",
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

    conn = await asyncpg.connect(test_db_base_url)
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
