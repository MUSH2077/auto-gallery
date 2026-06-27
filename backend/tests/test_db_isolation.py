"""Guards the fix for the production-data-loss incident: the test suite must
never connect to the production database. These checks are read-only."""

import os
from urllib.parse import urlparse

import pytest


def test_database_url_is_isolated_test_db():
    name = urlparse(os.environ["DATABASE_URL"]).path.lstrip("/")
    assert name.endswith("_test"), f"tests must use a *_test DB, got {name!r}"
    assert name != "autogallery", "tests must not target the production database"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_app_session_is_bound_to_test_db():
    """The app's own engine/session must actually talk to the *_test database,
    proving any data-mutating test hits the test DB, not production."""
    from sqlalchemy import text
    from app.database import async_session

    async with async_session() as db:
        current = (await db.execute(text("SELECT current_database()"))).scalar()
    assert current.endswith("_test"), f"app session is on {current!r}, not a *_test DB"
    assert current != "autogallery"
