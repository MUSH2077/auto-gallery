"""WebSocket task updates authenticated by single-use tickets and exact browser origins."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError

from app.config import settings
from app.services.browser_sessions import SessionStoreUnavailable, load_browser_session, password_fingerprint
from app.auth import _load_active_user_by_id
from app.services.ws_tickets import BrowserTicket, consume_ws_ticket
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)

# No router-level RequireAdmin — WebSocket upgrades cannot send custom headers.
# Admin auth is validated inline in the handler via JWT cookie payload.
router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Real-time task event stream using a single-use ticket."""
    allowed_origins = {value.strip() for value in settings.browser_session_origins.split(",") if value.strip()}
    if websocket.headers.get("origin") not in allowed_origins:
        await websocket.close(code=4003, reason="Untrusted origin")
        return
    ticket = websocket.query_params.get("ticket")
    if not ticket:
        await websocket.close(code=4001, reason="Missing ticket")
        return
    try:
        identity = consume_ws_ticket(ticket)
    except RedisError:
        await websocket.close(code=1013, reason="Ticket service unavailable")
        return
    if identity is None:
        await websocket.close(code=4001, reason="Invalid or expired ticket")
        return
    if isinstance(identity, BrowserTicket):
        try:
            browser_session = await load_browser_session(identity.session_token)
            if browser_session is None:
                raise ValueError("Session expired")
            user = await _load_active_user_by_id(browser_session.user_id)
            if (user.username != identity.username
                    or password_fingerprint(user.password_hash) != browser_session.password_fingerprint
                    or user.must_change_password):
                raise ValueError("Session is no longer valid")
        except (SessionStoreUnavailable, HTTPException, ValueError):
            await websocket.close(code=4001, reason="Browser session unavailable")
            return
        username = identity.username
    else:
        username = identity

    if (
        not isinstance(username, str)
        or not username
        or not await manager.is_current_tasks_user(username)
    ):
        await websocket.close(code=4003, reason="Missing task permission")
        return

    client_id = str(uuid4())
    await manager.connect(client_id, websocket, username=username)

    try:
        while True:
            if isinstance(identity, BrowserTicket):
                try:
                    current = await load_browser_session(identity.session_token)
                except SessionStoreUnavailable:
                    await websocket.close(code=1013, reason="Browser session unavailable")
                    break
                if current is None:
                    await websocket.close(code=4001, reason="Browser session expired")
                    break
            try:
                data = await asyncio.wait_for(websocket.receive_json(), timeout=30)
            except asyncio.TimeoutError:
                continue
            action = data.get("action", "")
            if action == "ping":
                await websocket.send_json({"type": "pong"})
            elif action == "subscribe":
                task_id = data.get("task_id")
                if task_id and await manager.can_subscribe(username, task_id):
                    await websocket.send_json({"type": "subscribed", "task_id": task_id})
                else:
                    # Identical for malformed, unknown, and unauthorized
                    # references so task existence is never disclosed.
                    await websocket.send_json({"type": "subscription_denied"})
            elif action == "unsubscribe":
                task_id = data.get("task_id")
                if task_id:
                    await websocket.send_json({"type": "unsubscribed", "task_id": task_id})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.debug("WebSocket error for client %s", client_id, exc_info=True)
    finally:
        await manager.disconnect(client_id)
