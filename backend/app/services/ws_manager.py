"""
WebSocket connection manager — bridges Redis pub/sub events to
browser WebSocket connections for real-time task status updates.

Security model (single-tenant NAS):
    The current deployment is single-admin. All authenticated clients
    receive all task events — there is no per-user event filtering.
    The connection manager does track which username each WebSocket
    belongs to, so event-scoping can be added later by checking
    ``connection_usernames[client_id]`` against an event's ``user`` field.

If multi-tenancy is added:
    1. Add a ``user`` field to published events
    2. In ``broadcast()``, skip clients whose username doesn't match

Architecture::

    Redis pub/sub ──► ConnectionManager (background thread)
                          │
                          ├──► WebSocket client A (user: admin)
                          ├──► WebSocket client B (user: admin)
                          └──► WebSocket client C
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.services.redis_client import get_redis
from app.services.redis_pubsub import TaskChannel

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections and forwards Redis pub/sub events.

    Currently broadcasts all events to all authenticated clients — this is
    intentional for the single-admin NAS deployment. Per-user scoping can be
    added later via ``connection_usernames``.
    """

    def __init__(self):
        self._connections: dict[str, WebSocket] = {}
        self._connection_usernames: dict[str, str] = {}
        self._lock = threading.Lock()
        self._running = False

    async def connect(self, client_id: str, websocket: WebSocket, *, username: str = "unknown") -> None:
        await websocket.accept()
        with self._lock:
            self._connections[client_id] = websocket
            self._connection_usernames[client_id] = username
        logger.info("WS client %s (user=%s) connected (%d total)", client_id, username, len(self._connections))
        await websocket.send_json({"type": "connected", "client_id": client_id})

    async def disconnect(self, client_id: str) -> None:
        with self._lock:
            self._connections.pop(client_id, None)
            self._connection_usernames.pop(client_id, None)
        logger.info("WS client %s disconnected (%d remaining)", client_id, len(self._connections))

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Send a message to all connected clients.

        Currently broadcasts to ALL clients (single-tenant model).
        To add per-user scoping: check message.user against self._connection_usernames.
        """
        disconnected: list[str] = []
        with self._lock:
            clients = list(self._connections.items())

        for client_id, ws in clients:
            try:
                await ws.send_json(message)
            except (WebSocketDisconnect, RuntimeError):
                disconnected.append(client_id)
            except Exception:
                disconnected.append(client_id)

        for cid in disconnected:
            await self.disconnect(cid)

    async def start_redis_listener(self) -> None:
        """Subscribe to task:all:events and broadcast to all clients.

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
