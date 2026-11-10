import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


def _default(o: Any):
    from datetime import datetime
    from decimal import Decimal

    if isinstance(o, Decimal):
        return format(o, "f")
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Not serialisable: {type(o)}")


class Broadcaster:
    """Fan-out of JSON events ``{"type": ..., "data": ...}`` to every connected dashboard."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def send(self, ws: WebSocket, event_type: str, data: Any) -> None:
        await ws.send_text(json.dumps({"type": event_type, "data": data}, default=_default))

    async def broadcast(self, event_type: str, data: Any) -> None:
        if not self._clients:
            return
        message = json.dumps({"type": event_type, "data": data}, default=_default)
        clients = list(self._clients)
        results = await asyncio.gather(*(c.send_text(message) for c in clients), return_exceptions=True)
        for client, result in zip(clients, results):
            if isinstance(result, Exception):
                self._clients.discard(client)
