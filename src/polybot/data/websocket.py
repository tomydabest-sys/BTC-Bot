"""WebSocket connection manager for real-time Polymarket data."""

from __future__ import annotations

import asyncio
import json

import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from polybot.events import EventBus

logger = structlog.get_logger()

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class WebSocketManager:
    """Manages WebSocket connections with auto-reconnect."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._subscriptions: set[str] = set()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._connection_loop())

    async def stop(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()

    async def subscribe_market(self, token_id: str) -> None:
        self._subscriptions.add(token_id)
        if self._ws:
            await self._send_subscribe(token_id)

    async def unsubscribe_market(self, token_id: str) -> None:
        self._subscriptions.discard(token_id)
        if self._ws:
            msg = json.dumps({"type": "unsubscribe", "channel": "book", "token_id": token_id})
            await self._ws.send(msg)

    async def _connection_loop(self) -> None:
        while self._running:
            try:
                async with websockets.connect(WS_URL) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1.0
                    logger.info("ws_connected")
                    # Resubscribe to all active markets
                    for token_id in self._subscriptions:
                        await self._send_subscribe(token_id)
                    await self._receive_loop(ws)
            except ConnectionClosed as e:
                logger.warning("ws_disconnected", code=e.code, reason=e.reason)
            except Exception as e:
                logger.error("ws_error", error=str(e))
            finally:
                self._ws = None
                if self._running:
                    logger.info("ws_reconnecting", delay=self._reconnect_delay)
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2, self._max_reconnect_delay
                    )

    async def _receive_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw_message in ws:
            try:
                message = json.loads(raw_message)
                await self._handle_message(message)
            except json.JSONDecodeError:
                logger.warning("ws_invalid_json", data=raw_message[:200])

    async def _handle_message(self, message: dict) -> None:
        msg_type = message.get("type", message.get("event_type", ""))
        if msg_type in ("book", "orderbook"):
            await self._event_bus.emit("orderbook_update", data=message)
        elif msg_type in ("trade", "last_trade_price"):
            await self._event_bus.emit("trade_update", data=message)
        elif msg_type == "heartbeat":
            pass  # Expected, ignore
        else:
            logger.debug("ws_unknown_message", type=msg_type)

    async def _send_subscribe(self, token_id: str) -> None:
        if self._ws:
            msg = json.dumps({"type": "subscribe", "channel": "book", "token_id": token_id})
            await self._ws.send(msg)
            logger.debug("ws_subscribed", token_id=token_id)
