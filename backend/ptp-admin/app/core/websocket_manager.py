"""
WebSocket 连接管理器

管理客户端连接和广播消息
"""

from typing import Dict, List, Set
from fastapi import WebSocket
import json
import logging

logger = logging.getLogger(__name__)

class ConnectionManager:
    """WebSocket 连接管理器"""

    def __init__(self):
        # 活跃连接 {user_id: WebSocket}
        self.active_connections: Dict[int, WebSocket] = {}
        # 房间成员 {room: Set[user_id]}
        self.rooms: Dict[str, Set[int]] = {}

    async def connect(self, websocket: WebSocket, user_id: int):
        """接受客户端连接"""
        await websocket.accept()
        self.active_connections[user_id] = websocket
        logger.info(f"User {user_id} connected via WebSocket")

    def disconnect(self, user_id: int):
        """断开客户端连接"""
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            logger.info(f"User {user_id} disconnected")

        # 从所有房间中移除
        for room in self.rooms:
            self.rooms[room].discard(user_id)

    async def send_personal_message(self, message: dict, user_id: int):
        """发送个人消息"""
        if user_id in self.active_connections:
            websocket = self.active_connections[user_id]
            try:
                await websocket.send_text(json.dumps(message, ensure_ascii=False))
                return True
            except Exception as e:
                logger.error(f"Failed to send message to user {user_id}: {e}")
                self.disconnect(user_id)
                return False
        return False

    async def broadcast(self, message: dict, room: str = None):
        """广播消息"""
        if room:
            # 发送到指定房间
            if room in self.rooms:
                disconnected_users = []
                for user_id in self.rooms[room]:
                    if user_id in self.active_connections:
                        success = await self.send_personal_message(message, user_id)
                        if not success:
                            disconnected_users.append(user_id)

                # 清理断开的连接
                for user_id in disconnected_users:
                    self.rooms[room].discard(user_id)
        else:
            # 广播给所有连接
            disconnected_users = []
            for user_id, websocket in list(self.active_connections.items()):
                success = await self.send_personal_message(message, user_id)
                if not success:
                    disconnected_users.append(user_id)

            # 清理断开的连接
            for user_id in disconnected_users:
                self.disconnect(user_id)

    def join_room(self, user_id: int, room: str):
        """用户加入房间"""
        if room not in self.rooms:
            self.rooms[room] = set()
        self.rooms[room].add(user_id)
        logger.info(f"User {user_id} joined room {room}")

    def leave_room(self, user_id: int, room: str):
        """用户离开房间"""
        if room in self.rooms:
            self.rooms[room].discard(user_id)
            logger.info(f"User {user_id} left room {room}")

    def get_room_members(self, room: str) -> Set[int]:
        """获取房间成员"""
        return self.rooms.get(room, set())

# 全局连接管理器实例
manager = ConnectionManager()