import pytest
from sqlalchemy import text


async def _clear_task_test_tables(db):
    await db.execute(text("""
        TRUNCATE task_events, task_runs RESTART IDENTITY CASCADE
    """))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_account_event_creates_terminal_listable_event():
    from app.database import async_session, engine
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            svc = TaskService(db)

            task = await svc.record_account_event(
                action="login", username="admin", user_id=1, ip="198.51.100.10"
            )
            await db.commit()

            assert task.kind == "account"
            assert task.operation_type == "account-login"
            assert task.status == "complete"
            assert task.subject_id is None
            assert task.meta == {"username": "admin", "user_id": 1, "ip": "198.51.100.10"}
            assert task.finished_at is not None

            total, tasks = await svc.list_tasks(kind="account")
            assert total == 1
            assert tasks[0].operation_type == "account-login"
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_account_event_password_change_distinct_rows():
    from app.database import async_session, engine
    from app.services.tasks import TaskService

    try:
        async with async_session() as db:
            await _clear_task_test_tables(db)
            svc = TaskService(db)

            await svc.record_account_event(action="login", username="admin", user_id=1)
            await svc.record_account_event(action="login", username="admin", user_id=1)
            await svc.record_account_event(action="password-change", username="admin", user_id=1)
            await db.commit()

            total, _ = await svc.list_tasks(kind="account")
            assert total == 3  # repeated logins are distinct rows, not upserted

            total_pw, pw = await svc.list_tasks(kind="account", operation_type="account-password-change")
            assert total_pw == 1
            assert pw[0].title == "Password changed"
    finally:
        async with async_session() as db:
            await _clear_task_test_tables(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_records_account_event_and_survives_recorder_failure(monkeypatch):
    from app.api import auth_api
    from app.api.auth_api import LoginRequest, login
    from app.auth import hash_password
    from app.database import async_session, engine
    from app.models.user import User
    from app.services.tasks import TaskService
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

            svc = TaskService(db)
            total, tasks = await svc.list_tasks(kind="account", operation_type="account-login")
            assert total == 1
            assert tasks[0].meta.get("username") == "acct_test"
            assert tasks[0].meta.get("ip") == "198.51.100.10"

            # Recorder failure must not break login
            async def _boom(**_kw):
                raise RuntimeError("recorder down")
            monkeypatch.setattr(TaskService, "record_account_event", _boom)
            resp2 = await login(LoginRequest(username="acct_test", password="secret123"), _req(), db)
            assert resp2.access_token  # login still succeeds
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM users WHERE username = 'acct_test'"))
            await _clear_task_test_tables(db)
            await db.commit()
        await engine.dispose()
