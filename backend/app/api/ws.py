"""
WebSocket endpoint for real-time task status updates.

Authentication uses the JWT cookie (``ag_token``) set by the admin-web login
flow. The browser sends this cookie automatically on the WebSocket upgrade
request, avoiding token leakage in server access logs that would occur with
query-parameter tokens.

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
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)

# No router-level RequireAdmin — WebSocket upgrades cannot send custom headers.
# Admin auth is validated inline in the handler via JWT cookie payload.
router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Real-time task event stream. Authenticated via JWT cookie."""
    # Read token from cookie (set by admin-web login, stored as ag_token)
    token = websocket.cookies.get("ag_token")
    if not token:
        await websocket.close(code=4001, reason="Missing auth cookie")
        return

    payload = decode_access_token_payload(token)
    if not payload:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    username = payload.get("sub", "unknown")
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
