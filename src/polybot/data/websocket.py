"""WebSocket connection manager for real-time Polymarket data.

Fix: Polymarket CLOB WS requires subscribe messages in this exact format:
  {"assets_ids": ["<token_id>"], "type": "market"}
The old format {"type": "subscribe", "channel": "book", "token_id": "..."} 
returns 'INVALID OPERATION' which is what was showing in logs.
"""

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
            # Polymarket doesn't have an explicit unsubscribe — we just stop tracking
            logger.debug("ws_unsubscribed", token_id=token_id[:16])

    async def _connection_loop(self) -> None:
        while self._running:
            try:
                async with websockets.connect(
                    WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1.0
                    logger.info("ws_connected", url=WS_URL)
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
                # Polymarket sends a list of events or a single dict
                if isinstance(message, list):
                    for item in message:
                        await self._handle_message(item)
                else:
                    await self._handle_message(message)
            except json.JSONDecodeError:
                logger.warning("ws_invalid_json", data=str(raw_message)[:200])

    async def _handle_message(self, message: dict) -> None:
        msg_type = message.get("event_type", message.get("type", ""))

        if msg_type == "book":
            await self._event_bus.emit("orderbook_update", data=message)
        elif msg_type in ("price_change", "last_trade_price"):
            await self._event_bus.emit("trade_update", data=message)
        elif msg_type == "tick_size_change":
            pass  # Informational, ignore
        else:
            logger.debug("ws_unknown_message", type=msg_type, preview=str(message)[:100])

    async def _send_subscribe(self, token_id: str) -> None:
        """Send subscribe in Polymarket's required format.

        Correct format (from Polymarket docs):
          {"assets_ids": ["<token_id>"], "type": "market"}

        The old format {"type": "subscribe", "channel": "book", "token_id": "..."}
        was returning 'INVALID OPERATION'.
        """
        if self._ws:
            msg = json.dumps({
                "type": "market",
                "assets_ids": [token_id],
            })
            await self._ws.send(msg)
            logger.info("ws_subscribed", token_id=token_id[:16])
