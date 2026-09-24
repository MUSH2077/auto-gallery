"""Durable task visibility on the realtime WebSocket boundary."""

from __future__ import annotations

from uuid import uuid4

import pytest


PREFIX = "ws_task_isolation_"


class RecordingWebSocket:
    def __init__(self, *, token: str | None = None, messages: list[dict] | None = None):
        self.cookies = {}
        self.headers = {"origin": "http://localhost:13000"}
        self.query_params: dict[str, str] = {"ticket": token} if token else {}
        self.messages = list(messages or [])
        self.sent: list[dict] = []
        self.accepted = False
        self.closed: list[tuple[int, str]] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append((code, reason))

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)

    async def receive_json(self) -> dict:
        from fastapi import WebSocketDisconnect

        if not self.messages:
            raise WebSocketDisconnect()
        return self.messages.pop(0)


async def _cleanup(db) -> None:
    from sqlalchemy import text

    await db.execute(
        text(
            "DELETE FROM task_events WHERE task_run_id IN ("
            "SELECT id FROM task_runs WHERE title LIKE :prefix)"
        ),
        {"prefix": f"{PREFIX}%"},
    )
    await db.execute(
        text("DELETE FROM task_runs WHERE title LIKE :prefix"),
        {"prefix": f"{PREFIX}%"},
    )
    await db.execute(
        text(
            "DELETE FROM download_jobs WHERE subscription_id IN ("
            "SELECT s.id FROM subscriptions s JOIN creators c ON c.id=s.creator_id "
            "WHERE c.name LIKE :prefix)"
        ),
        {"prefix": f"{PREFIX}%"},
    )
    await db.execute(
        text(
            "DELETE FROM user_subscriptions WHERE subscription_id IN ("
            "SELECT s.id FROM subscriptions s JOIN creators c ON c.id=s.creator_id "
            "WHERE c.name LIKE :prefix)"
        ),
        {"prefix": f"{PREFIX}%"},
    )
    await db.execute(
        text(
            "DELETE FROM subscription_sources WHERE subscription_id IN ("
            "SELECT s.id FROM subscriptions s JOIN creators c ON c.id=s.creator_id "
            "WHERE c.name LIKE :prefix)"
        ),
        {"prefix": f"{PREFIX}%"},
    )
    await db.execute(
        text(
            "DELETE FROM subscriptions WHERE creator_id IN ("
            "SELECT id FROM creators WHERE name LIKE :prefix)"
        ),
        {"prefix": f"{PREFIX}%"},
    )
    await db.execute(
        text("DELETE FROM creators WHERE name LIKE :prefix"),
        {"prefix": f"{PREFIX}%"},
    )
    await db.execute(
        text("DELETE FROM users WHERE username LIKE :prefix"),
        {"prefix": f"{PREFIX}%"},
    )
    await db.commit()


async def _seed(db):
    from app.models import (
        Creator,
        DownloadJob,
        Subscription,
        SubscriptionSource,
        TaskRun,
        User,
        UserSubscription,
    )

    marker = f"{PREFIX}{uuid4().hex}"
    owner = User(
        username=f"{marker}_owner",
        password_hash="test-only",
        is_active=True,
        permissions=["tasks"],
    )
    member = User(
        username=f"{marker}_member",
        password_hash="test-only",
        is_active=True,
        permissions=["tasks"],
    )
    admin = User(
        username=f"{marker}_admin",
        password_hash="test-only",
        is_active=True,
        is_admin=True,
    )
    no_tasks = User(
        username=f"{marker}_no_tasks",
        password_hash="test-only",
        is_active=True,
        permissions=["library"],
    )
    creator = Creator(name=marker)
    db.add_all([owner, member, admin, no_tasks, creator])
    await db.flush()
    subscription = Subscription(creator_id=creator.id, name=marker)
    db.add(subscription)
    await db.flush()
    source = SubscriptionSource(
        subscription_id=subscription.id,
        source="pixiv",
        source_creator_id=f"ws-{uuid4().hex}",
        source_url=f"https://example.invalid/source/{uuid4().hex}",
    )
    db.add(source)
    await db.flush()
    owner_membership = UserSubscription(
        user_id=owner.id,
        subscription_id=subscription.id,
        name=marker,
    )
    member_membership = UserSubscription(
        user_id=member.id,
        subscription_id=subscription.id,
        name=marker,
    )
    db.add_all([owner_membership, member_membership])
    await db.flush()

    private_job = DownloadJob(
        subscription_id=subscription.id,
        subscription_source_id=source.id,
        triggering_user_subscription_id=owner_membership.id,
        owner_user_id=owner.id,
        source="pixiv",
        source_url="https://example.invalid/private",
        status="running",
    )
    legacy_job = DownloadJob(
        subscription_id=subscription.id,
        subscription_source_id=source.id,
        owner_user_id=None,
        source="pixiv",
        source_url="https://example.invalid/legacy",
        status="running",
    )
    db.add_all([private_job, legacy_job])
    await db.flush()
    private_task = TaskRun(
        kind="download",
        subject_type="download_job",
        subject_id=private_job.id,
        owner_user_id=owner.id,
        triggering_user_subscription_id=owner_membership.id,
        status="running",
        resource_state="waiting",
        title=f"{marker}_private",
    )
    legacy_task = TaskRun(
        kind="download",
        subject_type="download_job",
        subject_id=legacy_job.id,
        owner_user_id=None,
        status="running",
        resource_state="waiting",
        title=f"{marker}_legacy",
    )
    global_task = TaskRun(
        kind="admin",
        operation_type="unregistered-global-operation",
        owner_user_id=None,
        status="running",
        resource_state="waiting",
        title=f"{marker}_global",
    )
    restricted_task = TaskRun(
        kind="admin",
        operation_type="admin-clear",
        owner_user_id=None,
        status="running",
        resource_state="waiting",
        title=f"{marker}_restricted",
    )
    db.add_all([private_task, legacy_task, global_task, restricted_task])
    await db.commit()
    return {
        "owner": owner,
        "member": member,
        "admin": admin,
        "no_tasks": no_tasks,
        "owner_membership": owner_membership,
        "private_job": private_job,
        "legacy_job": legacy_job,
        "private_task": private_task,
        "legacy_task": legacy_task,
        "global_task": global_task,
        "restricted_task": restricted_task,
    }


def _event(task_id, task_type="download") -> dict:
    return {
        "type": "status_change",
        "task_id": str(task_id),
        "task_type": task_type,
        "old_status": "running",
        "new_status": "complete",
        "operator": "private-operator",
        "note": "private-note",
        "progress": {"stage": "private-progress"},
        "timestamp": "2026-08-29T00:00:00+00:00",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_broadcast_matches_rest_owner_legacy_and_permission_visibility():
    """Global Redis events must never cross durable REST task visibility."""

    from sqlalchemy import delete

    from app.database import async_session, engine
    from app.models import User, UserSubscription
    from app.services.ws_manager import ConnectionManager

    sockets: dict[str, RecordingWebSocket] = {}
    manager = ConnectionManager()
    try:
        async with async_session() as db:
            await _cleanup(db)
            rows = await _seed(db)
            usernames = {
                role: rows[role].username
                for role in ("owner", "member", "admin", "no_tasks")
            }
            ids = {key: value.id for key, value in rows.items() if hasattr(value, "id")}

        for role, username in usernames.items():
            socket = RecordingWebSocket()
            sockets[role] = socket
            await manager.connect(role, socket, username=username)
            socket.sent.clear()

        # Publishers use both the domain subject id and the TaskRun id.
        await manager.broadcast(_event(ids["private_job"]))
        assert [role for role, socket in sockets.items() if socket.sent] == ["owner"]
        for socket in sockets.values():
            socket.sent.clear()
        await manager.broadcast(_event(ids["private_task"]))
        assert [role for role, socket in sockets.items() if socket.sent] == ["owner"]

        for socket in sockets.values():
            socket.sent.clear()
        await manager.broadcast(_event(ids["legacy_job"]))
        assert [role for role, socket in sockets.items() if socket.sent] == [
            "owner",
            "member",
        ]

        for socket in sockets.values():
            socket.sent.clear()
        await manager.broadcast(_event(ids["global_task"], "admin"))
        assert [role for role, socket in sockets.items() if socket.sent] == [
            "owner",
            "member",
            "admin",
        ]

        for socket in sockets.values():
            socket.sent.clear()
        await manager.broadcast(_event(ids["restricted_task"], "admin"))
        assert [role for role, socket in sockets.items() if socket.sent] == ["admin"]

        async with async_session() as db:
            await db.execute(
                delete(UserSubscription).where(
                    UserSubscription.id == ids["owner_membership"]
                )
            )
            await db.commit()
        for socket in sockets.values():
            socket.sent.clear()
        await manager.broadcast(_event(ids["legacy_job"]))
        assert [role for role, socket in sockets.items() if socket.sent] == ["member"]
        for socket in sockets.values():
            socket.sent.clear()
        await manager.broadcast(_event(ids["private_job"]))
        assert [role for role, socket in sockets.items() if socket.sent] == ["owner"]

        async with async_session() as db:
            await db.execute(delete(User).where(User.id == ids["owner"]))
            await db.commit()
        for socket in sockets.values():
            socket.sent.clear()
        await manager.broadcast(_event(ids["private_job"]))
        assert all(socket.sent == [] for socket in sockets.values())

        for malformed in (
            _event("not-a-uuid"),
            {"type": "status_change", "task_id": str(uuid4())},
            _event(uuid4()),
            {"type": "unknown", "task_id": str(ids["legacy_task"]), "task_type": "download"},
        ):
            await manager.broadcast(malformed)
        assert all(socket.sent == [] for socket in sockets.values())
    finally:
        for client_id in list(sockets):
            await manager.disconnect(client_id)
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscribe_ack_requires_current_visible_task_without_existence_leak(
    monkeypatch,
):
    """Unknown, malformed, and foreign task references get one generic denial."""

    from app.api import ws as ws_api
    from app.database import async_session, engine

    try:
        async with async_session() as db:
            await _cleanup(db)
            rows = await _seed(db)
            username = rows["member"].username
            private_id = rows["private_task"].id
            legacy_id = rows["legacy_task"].id

        monkeypatch.setattr(
            ws_api, "consume_ws_ticket",
            lambda ticket: username if ticket == "member-token" else None,
        )
        socket = RecordingWebSocket(
            token="member-token",
            messages=[
                {"action": "subscribe", "task_id": str(private_id)},
                {"action": "subscribe", "task_id": "malformed-reference"},
                {"action": "subscribe", "task_id": str(uuid4())},
                {"action": "subscribe", "task_id": str(legacy_id)},
            ],
        )
        await ws_api.websocket_endpoint(socket)

        subscribe_replies = [
            message
            for message in socket.sent
            if message.get("type") in {"subscribed", "subscription_denied"}
        ]
        assert subscribe_replies == [
            {"type": "subscription_denied"},
            {"type": "subscription_denied"},
            {"type": "subscription_denied"},
            {"type": "subscribed", "task_id": str(legacy_id)},
        ]
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_websocket_rejects_active_user_without_tasks_permission(
    monkeypatch,
):
    """Authentication alone cannot bypass the REST tasks module permission."""

    from app.api import ws as ws_api
    from app.database import async_session, engine

    try:
        async with async_session() as db:
            await _cleanup(db)
            rows = await _seed(db)
            username = rows["no_tasks"].username

        monkeypatch.setattr(
            ws_api, "consume_ws_ticket",
            lambda ticket: username if ticket == "no-tasks-ticket" else None,
        )
        socket = RecordingWebSocket(token="no-tasks-ticket")
        await ws_api.websocket_endpoint(socket)

        assert socket.accepted is False
        assert socket.sent == []
        assert socket.closed == [(4003, "Missing task permission")]
    finally:
        async with async_session() as db:
            await _cleanup(db)
        await engine.dispose()
