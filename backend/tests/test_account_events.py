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
