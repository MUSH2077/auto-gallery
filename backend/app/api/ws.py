"""
WebSocket endpoint for real-time task status updates.

Clients connect with a JWT token as a query parameter::

    ws://host:8000/api/v1/ws?token=<jwt>

The server broadcasts all task events (status changes, progress updates,
heartbeats) to every connected client.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.auth import decode_access_token_payload
from app.services.ws_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(...),
):
    """Real-time task event stream. Requires JWT auth via query parameter."""
    payload = decode_access_token_payload(token)
    if not payload:
        await websocket.close(code=4001, reason="Invalid or expired token")
        return

    client_id = str(uuid4())
    await manager.connect(client_id, websocket)

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
