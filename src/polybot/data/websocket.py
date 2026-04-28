"""WebSocket connection manager for real-time Polymarket data.

PATCHED FROM ORIGINAL:
1. Silent-freeze watchdog: if no `book`/`price_change` event in 60s, force reconnect.
   This addresses py-clob-client #292 — connection looks alive (PING/PONG works) but
   server stops sending book deltas.
2. Stamps polymarket_book health on every successful book event.
3. Logs ws_book_event for diagnostics — count this in PowerShell to verify
   data is actually flowing.
"""

from __future__ import annotations

import asyncio
import json
import time

import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from polybot.events import EventBus
from polybot.health_monitor import get_monitor

logger = structlog.get_logger()

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Watchdog threshold: if no book event in this many seconds, force reconnect
SILENT_FREEZE_THRESHOLD_S = 60.0
WATCHDOG_INTERVAL_S = 10.0


class WebSocketManager:
    """Manages WebSocket connections with auto-reconnect AND silent-freeze detection."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._subscriptions: set[str] = set()
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
        # Silent-freeze watchdog state
        self._last_data_ts = 0.0
        self._book_events_received = 0
        self._force_reconnect = asyncio.Event()
        self._watchdog_task: asyncio.Task | None = None
        self._health = get_monitor()

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._connection_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop(self) -> None:
        self._running = False
        self._force_reconnect.set()
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            await self._ws.close()

    async def subscribe_market(self, token_id: str) -> None:
        self._subscriptions.add(token_id)
        if self._ws:
            await self._send_subscribe(token_id)

    async def unsubscribe_market(self, token_id: str) -> None:
        self._subscriptions.discard(token_id)
        if self._ws:
            logger.debug("ws_unsubscribed", token_id=token_id[:16])

    @property
    def book_events_received(self) -> int:
        """Diagnostic: total book events received this process lifetime."""
        return self._book_events_received

    async def _watchdog_loop(self) -> None:
        """Detect py-clob-client #292 silent-freeze: WS alive but no book events."""
        await asyncio.sleep(15)  # Grace period after startup
        while self._running:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
                if self._last_data_ts == 0:
                    # Never received any data yet, but WS connected
                    if self._ws is not None and self._subscriptions:
                        logger.warning(
                            "ws_no_data_since_connect",
                            subscriptions=len(self._subscriptions),
                            elapsed_s=round(time.time() - self._last_data_ts, 1)
                            if self._last_data_ts else "never",
                        )
                    continue
                age = time.time() - self._last_data_ts
                if age > SILENT_FREEZE_THRESHOLD_S:
                    logger.warning(
                        "ws_silent_freeze_detected",
                        age_s=round(age, 1),
                        threshold_s=SILENT_FREEZE_THRESHOLD_S,
                        book_events_total=self._book_events_received,
                        action="forcing_reconnect",
                    )
                    self._force_reconnect.set()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("ws_watchdog_err", error=str(e))

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
                    self._force_reconnect.clear()
                    logger.info("ws_connected", url=WS_URL,
                                subscriptions=len(self._subscriptions))
                    for token_id in self._subscriptions:
                        await self._send_subscribe(token_id)

                    receive_task = asyncio.create_task(self._receive_loop(ws))
                    reconnect_task = asyncio.create_task(self._force_reconnect.wait())

                    done, pending = await asyncio.wait(
                        {receive_task, reconnect_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for task in pending:
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass

                    if self._force_reconnect.is_set():
                        logger.info("ws_force_reconnect_triggered")

            except ConnectionClosed as e:
                logger.warning("ws_disconnected", code=e.code, reason=str(e.reason))
            except Exception as e:
                logger.error("ws_error", error=str(e), error_type=type(e).__name__)
            finally:
                self._ws = None
                if self._running:
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(
                        self._reconnect_delay * 1.5, self._max_reconnect_delay
                    )

    async def _receive_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw_message in ws:
            try:
                # Update last-data timestamp on EVERY inbound message
                self._last_data_ts = time.time()
                message = json.loads(raw_message)
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
            self._book_events_received += 1
            self._health.stamp("polymarket_book")
            if self._book_events_received in (1, 10, 100, 1000):
                logger.info("ws_book_event_milestone",
                            count=self._book_events_received)
            await self._event_bus.emit("orderbook_update", data=message)
        elif msg_type in ("price_change", "last_trade_price"):
            self._health.stamp("polymarket_book")
            await self._event_bus.emit("trade_update", data=message)
        elif msg_type == "tick_size_change":
            pass
        else:
            logger.debug("ws_unknown_message", type=msg_type,
                         preview=str(message)[:100])

    async def _send_subscribe(self, token_id: str) -> None:
        """Send subscribe in Polymarket's required format."""
        if self._ws:
            msg = json.dumps({
                "type": "market",
                "assets_ids": [token_id],
            })
            await self._ws.send(msg)
            logger.info("ws_subscribed", token_id=token_id[:16])
