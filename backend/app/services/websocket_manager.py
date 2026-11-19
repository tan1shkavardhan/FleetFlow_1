import asyncio
import logging

from fastapi import WebSocket


logger = logging.getLogger(__name__)


class WebSocketManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()

        async with self._lock:
            self._connections.add(websocket)

        logger.info(
            "WebSocket connected. Active connections: %s",
            self.connection_count,
        )

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

        logger.info(
            "WebSocket disconnected. Active connections: %s",
            self.connection_count,
        )

    async def broadcast(self, message: dict) -> None:
        async with self._lock:
            peers = list(self._connections)

        disconnected = []

        for peer in peers:
            try:
                await peer.send_json(message)
            except Exception:
                disconnected.append(peer)

        if disconnected:
            async with self._lock:
                for peer in disconnected:
                    self._connections.discard(peer)

            logger.warning(
                "Removed %s disconnected WebSocket clients",
                len(disconnected),
            )

    @property
    def connection_count(self) -> int:
        return len(self._connections)


manager = WebSocketManager()