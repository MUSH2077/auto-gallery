"""WebSocket connection manager for owner-filtered realtime task updates.

Redis payloads deliberately carry no user or credential material. Every event
reference is instead resolved to its durable TaskRun and checked against the
same owner-first, bounded-legacy visibility used by the REST task detail API.

Architecture::

    Redis pub/sub ──► ConnectionManager (background thread)
                          │
                          ├──► authorized WebSocket client A
                          └──► authorized WebSocket client B
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import and_, or_, select

from app.database import async_session
from app.models import TaskRun, User
from app.services.operations import (
    admin_operation_required_permission,
    can_access_admin_operation,
)
from app.services.redis_client import get_redis
from app.services.redis_pubsub import TaskChannel
from app.services.tasks import (
    TaskService,
    can_access_global_subscription_batch,
    is_global_subscription_batch,
)

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manage WebSocket connections and fail-closed task event forwarding."""

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}
        self._connection_accounts: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._running = False

    async def connect(self, client_id: str, websocket: WebSocket, *, account: tuple[int, int], username: str) -> None:
        await websocket.accept()
        with self._lock:
            self._connections[client_id] = websocket
            self._connection_accounts[client_id] = account
        logger.info("WS client %s (user=%s) connected (%d total)", client_id, username, len(self._connections))
        await websocket.send_json({"type": "connected", "client_id": client_id})

    async def disconnect(self, client_id: str) -> None:
        with self._lock:
            self._connections.pop(client_id, None)
            self._connection_accounts.pop(client_id, None)
        logger.info("WS client %s disconnected (%d remaining)", client_id, len(self._connections))

    @staticmethod
    async def _current_tasks_user(db, account: tuple[int, int]) -> User | None:
        user = (
            await db.execute(
                select(User).where(
                    User.id == account[0],
                    User.credential_version == account[1],
                    User.is_active.is_(True),
                    User.must_change_password.is_(False),
                )
            )
        ).scalar_one_or_none()
        if user is None or not (user.is_admin or "tasks" in set(user.permissions or ())):
            return None
        return user

    @staticmethod
    async def _resolve_task_reference(
        db,
        raw_task_id: Any,
        *,
        task_type: str | None = None,
    ) -> TaskRun | None:
        try:
            task_id = UUID(str(raw_task_id))
        except (TypeError, ValueError, AttributeError):
            return None

        exact = TaskRun.id == task_id
        subject = None
        if task_type in {"download", "import"}:
            subject = and_(
                TaskRun.subject_type == f"{task_type}_job",
                TaskRun.subject_id == task_id,
            )
        locator = or_(exact, subject) if subject is not None else exact
        stmt = select(TaskRun).where(locator)
        if task_type is not None:
            stmt = stmt.where(TaskRun.kind == task_type)
        tasks = list((await db.execute(stmt.limit(2))).scalars().unique())
        return tasks[0] if len(tasks) == 1 else None

    @staticmethod
    async def _task_visible_to_user(db, task: TaskRun, user: User) -> bool:
        service = TaskService(db)
        if task.owner_user_id is not None:
            return await service.is_visible_to_user(task, user.id)
        if is_global_subscription_batch(task):
            return can_access_global_subscription_batch(user)
        if (
            task.kind == "admin"
            and admin_operation_required_permission(task.operation_type) is not None
        ):
            return can_access_admin_operation(user, task.operation_type)
        return await service.is_visible_to_user(task, user.id)

    async def is_current_tasks_user(self, account: tuple[int, int]) -> bool:
        """Revalidate active-user and tasks-module access before accepting."""

        async with async_session() as db:
            return await self._current_tasks_user(db, account) is not None

    async def can_subscribe(self, account: tuple[int, int], task_id: Any) -> bool:
        """Authorize a TaskRun reference without disclosing its existence."""

        try:
            async with async_session() as db:
                user = await self._current_tasks_user(db, account)
                task = await self._resolve_task_reference(db, task_id)
                return bool(
                    user is not None
                    and task is not None
                    and await self._task_visible_to_user(db, task, user)
                )
        except Exception:
            logger.warning("WebSocket task subscription visibility check failed")
            return False

    @staticmethod
    def _is_task_event(message: dict[str, Any]) -> bool:
        return bool(
            isinstance(message, dict)
            and message.get("type") in {"status_change", "progress"}
            and isinstance(message.get("task_type"), str)
            and message.get("task_id") is not None
        )

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Resolve one event durably and send it only to currently visible users."""

        if not self._is_task_event(message):
            return
        disconnected: list[str] = []
        with self._lock:
            clients = [
                (client_id, websocket, self._connection_accounts.get(client_id))
                for client_id, websocket in self._connections.items()
            ]

        authorized: set[tuple[int, int]] = set()
        try:
            async with async_session() as db:
                task = await self._resolve_task_reference(
                    db,
                    message.get("task_id"),
                    task_type=message.get("task_type"),
                )
                if task is None:
                    return
                for _client_id, _websocket, account in clients:
                    if not account or account in authorized:
                        continue
                    user = await self._current_tasks_user(db, account)
                    if user is not None and await self._task_visible_to_user(db, task, user):
                        authorized.add(account)
        except Exception:
            logger.warning("WebSocket task event visibility check failed")
            return

        for client_id, ws, account in clients:
            if account not in authorized:
                continue
            try:
                await ws.send_json(message)
            except (WebSocketDisconnect, RuntimeError):
                disconnected.append(client_id)
            except Exception:
                disconnected.append(client_id)

        for cid in disconnected:
            await self.disconnect(cid)

    async def start_redis_listener(self) -> None:
        """Subscribe to task:all:events and forward owner-filtered messages.

        The Redis pub/sub listener runs in a daemon thread and calls
        ``broadcast()`` via ``run_coroutine_threadsafe`` — no blocking
        synchronous I/O on the event loop thread. The async task itself
        just sleeps so it can be cancelled gracefully on shutdown.
        """
        self._running = True
        loop = asyncio.get_running_loop()

        def _reader():
            r = get_redis()
            pubsub = r.pubsub()
            pubsub.subscribe(TaskChannel.all_events())
            logger.info("Redis pub/sub listener started")
            for message in pubsub.listen():
                if not self._running:
                    break
                if message["type"] != "message":
                    continue
                try:
                    data = json.loads(message["data"])
                    asyncio.run_coroutine_threadsafe(self.broadcast(data), loop)
                except (json.JSONDecodeError, TypeError):
                    pass
                except RuntimeError:
                    pass
            pubsub.unsubscribe()
            pubsub.close()
            logger.info("Redis pub/sub listener stopped")

        reader_thread = threading.Thread(target=_reader, daemon=True, name="ws-redis")
        reader_thread.start()

        try:
            # Keep the async task alive — all real work happens in _reader thread
            while self._running:
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            self._running = False
            reader_thread.join(timeout=5)
            logger.info("WebSocket Redis listener stopped")


manager = ConnectionManager()
