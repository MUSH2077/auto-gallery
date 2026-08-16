import inspect

import pytest
from sqlalchemy import func, select, text


async def _clear_task_test_tables(db):
    await db.execute(text("""
        TRUNCATE task_events, task_runs RESTART IDENTITY CASCADE
    """))
    await db.commit()


def test_auth_success_paths_do_not_depend_on_task_history():
    from app.api import auth_api
    from app.services.tasks import TaskService

    source = inspect.getsource(auth_api)
    assert "TaskService" not in source
    assert not hasattr(TaskService, "record_account_event")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_updates_user_without_creating_account_task(monkeypatch):
    from app.api import auth_api
    from app.api.auth_api import LoginRequest, login
    from app.auth import hash_password
    from app.database import async_session, engine
    from app.models.user import User
    from app.models.task_run import TaskRun
    from starlette.requests import Request

    def _req(ip: str = "198.51.100.10") -> Request:
        return Request({
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": "/api/v1/auth/login", "query_string": b"",
            "headers": [], "client": (ip, 5000), "server": ("testserver", 80),
        })

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            await db.execute(text("DELETE FROM users WHERE username = 'acct_test'"))
            db.add(User(username="acct_test", password_hash=hash_password("secret123"),
                        is_active=True, must_change_password=False))
            await db.commit()

            # Bypass the Redis rate limiter for the test
            async def _ok(_ip):
                return True, 5, 0
            monkeypatch.setattr(auth_api._login_limiter, "check", _ok)

            resp = await login(LoginRequest(username="acct_test", password="secret123"), _req(), db)
            assert resp.access_token

            task_count = int(
                (
                    await db.execute(
                        select(func.count(TaskRun.id)).where(TaskRun.kind == "account")
                    )
                ).scalar_one()
            )
            refreshed = (
                await db.execute(select(User).where(User.username == "acct_test"))
            ).scalar_one()
            assert task_count == 0
            assert refreshed.last_login_at is not None
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = 'acct_test'"))
            await _clear_task_test_tables(db)
            await db.commit()
        await engine.dispose()
