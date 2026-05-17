"""
WebSocket API routes for the public alpha build.
"""

from typing import Optional
import json
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.database import SessionLocal
from app.core.websocket_manager import manager
from app.services.auth_service import AuthService

router = APIRouter()
logger = logging.getLogger(__name__)

WS_CLOSE_POLICY_VIOLATION = 1008


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(None),
    room: Optional[str] = Query(None),
):
    user_id: Optional[int] = None
    db = None

    try:
        if not token:
            await websocket.close(
                code=WS_CLOSE_POLICY_VIOLATION, reason="Missing authentication token"
            )
            return

        db = SessionLocal()
        auth_service = AuthService(db)
        user_id = auth_service.verify_token(token)
        if user_id is None:
            await websocket.close(
                code=WS_CLOSE_POLICY_VIOLATION, reason="Invalid or expired token"
            )
            return

        user = auth_service.get_user_by_id(user_id)
        if user is None or not user.is_active:
            await websocket.close(
                code=WS_CLOSE_POLICY_VIOLATION, reason="User not found or inactive"
            )
            return

        await manager.connect(websocket, user_id)

        if room:
            manager.join_room(user_id, room)
            await websocket.send_json({"type": "room_joined", "room": room})

        while True:
            data = await websocket.receive_text()
            try:
                message_data = json.loads(data)
                logger.info("Received message from user %s: %s", user_id, message_data)
            except json.JSONDecodeError as exc:
                logger.warning("Invalid JSON from user %s: %s", user_id, exc)
                await websocket.send_json(
                    {"type": "error", "message": "Invalid JSON format"}
                )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for user %s", user_id)
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
    finally:
        if user_id is not None:
            manager.disconnect(user_id)
            if room:
                manager.leave_room(user_id, room)
        if db is not None:
            db.close()


@router.post("/ws/test")
async def test_websocket(
    message: dict,
    user_id: Optional[int] = Query(None, description="目标用户ID，不指定则广播"),
    room: Optional[str] = Query(None, description="目标房间"),
):
    message["timestamp"] = "2025-12-11T00:00:00Z"

    if user_id:
        await manager.send_personal_message(message, user_id)
        return {"status": "sent", "target": f"user_{user_id}"}
    if room:
        await manager.broadcast(message, room)
        return {"status": "sent", "target": f"room_{room}"}
    await manager.broadcast(message)
    return {"status": "broadcasted", "target": "all"}


@router.get("/ws/rooms/{room}/members")
async def get_room_members(room: str):
    members = manager.get_room_members(room)
    return {"room": room, "members": list(members), "count": len(members)}
