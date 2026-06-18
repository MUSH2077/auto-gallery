"""
WebSocket endpoint for real-time task status updates.

Authentication prefers the JWT cookie (``ag_token``) set by the admin-web
login flow. The browser sends this cookie automatically on the WebSocket
upgrade request. A short-lived one-time ``?ticket=`` fallback is accepted for
cross-port or reverse-proxy deployments where the cookie is not visible to the
backend.

The server broadcasts all task events to every authenticated client.
This is intentional — the current deployment is single-admin NAS and the
global event stream is the simplest correct model. If multi-tenant support
is added in the future, scope broadcasts by user/tenant.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth import decode_access_token_payload
from app.services.ws_tickets import consume_ws_ticket
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)

# No router-level RequireAdmin — WebSocket upgrades cannot send custom headers.
# Admin auth is validated inline in the handler via JWT cookie payload.
router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Real-time task event stream. Authenticated via JWT cookie or ticket."""
    username = "unknown"
    token = websocket.cookies.get("ag_token")
    if token:
        payload = decode_access_token_payload(token)
        if not payload:
            await websocket.close(code=4001, reason="Invalid or expired token")
            return
        username = payload.get("sub", "unknown")
    else:
        ticket = websocket.query_params.get("ticket")
        if not ticket:
            await websocket.close(code=4001, reason="Missing auth cookie or ticket")
            return
        ticket_username = consume_ws_ticket(ticket)
        if not ticket_username:
            await websocket.close(code=4001, reason="Invalid or expired ticket")
            return
        username = ticket_username

    client_id = str(uuid4())
    await manager.connect(client_id, websocket, username=username)

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action", "")
            if action == "ping":
                await websocket.send_json({"type": "pong"})
            elif action == "subscribe":
                task_id = data.get("task_id")
                if task_id:
                    await websocket.send_json({"type": "subscribed", "task_id": task_id})
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
