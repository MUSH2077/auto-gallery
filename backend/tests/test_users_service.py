import pytest
from sqlalchemy import text


async def _clear_test_users(db):
    await db.execute(text("DELETE FROM users WHERE username LIKE 'svc_test_%'"))
    await db.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_then_list_contains_new_user():
    from app.database import async_session, engine
    from app.services.users import UserService

    try:
        async with async_session() as db:
            await _clear_test_users(db)
            svc = UserService(db)

            user = await svc.create(username="svc_test_alice", password="hunter22", display_name="Alice")

            assert user.id is not None
            assert user.must_change_password is True
            assert user.is_admin is False
            assert user.permissions == []

            users = await svc.list()
            assert any(u.username == "svc_test_alice" for u in users)
    finally:
        async with async_session() as db:
            await _clear_test_users(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_create_duplicate_username_raises():
    from app.database import async_session, engine
    from app.services.users import UserService

    try:
        async with async_session() as db:
            await _clear_test_users(db)
            svc = UserService(db)
            await svc.create(username="svc_test_dup", password="hunter22")

            with pytest.raises(ValueError, match="username taken"):
                await svc.create(username="svc_test_dup", password="hunter22")
    finally:
        async with async_session() as db:
            await _clear_test_users(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_permissions_success():
    from app.database import async_session, engine
    from app.services.users import UserService

    try:
        async with async_session() as db:
            await _clear_test_users(db)
            svc = UserService(db)
            user = await svc.create(username="svc_test_bob", password="hunter22")

            updated = await svc.update(user.id, permissions=["library", "upload"])

            assert set(updated.permissions) == {"library", "upload"}
    finally:
        async with async_session() as db:
            await _clear_test_users(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_invalid_permission_module_raises():
    from app.database import async_session, engine
    from app.services.users import UserService

    try:
        async with async_session() as db:
            await _clear_test_users(db)
            svc = UserService(db)
            user = await svc.create(username="svc_test_carol", password="hunter22")

            with pytest.raises(ValueError):
                await svc.update(user.id, permissions=["not_a_real_module"])
    finally:
        async with async_session() as db:
            await _clear_test_users(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_last_admin_guard_blocks_demote_disable_delete():
    from app.database import async_session, engine
    from app.services.users import UserService

    try:
        async with async_session() as db:
            await _clear_test_users(db)
            svc = UserService(db)

            admin = await svc.create(username="svc_test_admin", password="hunter22", is_admin=True)

            with pytest.raises(ValueError, match="last admin"):
                await svc.update(admin.id, is_admin=False)

            with pytest.raises(ValueError, match="last admin"):
                await svc.update(admin.id, is_active=False)

            with pytest.raises(ValueError, match="last admin"):
                await svc.delete(admin.id)

            # Once a second active admin exists, the guard releases.
            await svc.create(username="svc_test_admin2", password="hunter22", is_admin=True)
            demoted = await svc.update(admin.id, is_admin=False)
            assert demoted.is_admin is False
    finally:
        async with async_session() as db:
            await _clear_test_users(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reset_password_returns_plaintext_and_flips_flag():
    from app.auth import verify_password
    from app.database import async_session, engine
    from app.services.users import UserService

    try:
        async with async_session() as db:
            await _clear_test_users(db)
            svc = UserService(db)
            user = await svc.create(username="svc_test_dave", password="hunter22")

            # Clear the flag first to prove reset_password flips it back on.
            user.must_change_password = False
            await db.commit()

            plaintext = await svc.reset_password(user.id)

            assert len(plaintext) >= 12
            refreshed = await svc.get(user.id)
            assert refreshed.must_change_password is True
            assert verify_password(plaintext, refreshed.password_hash)
    finally:
        async with async_session() as db:
            await _clear_test_users(db)
        await engine.dispose()
