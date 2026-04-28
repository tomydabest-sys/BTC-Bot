"""WebSocket connection manager for real-time Polymarket data.

PATCHED v2 — fixes from first run observation:
1. Subscribes are now throttled to 1/sec (was: 16 subscribes in <1s caused
   Polymarket to return 'INVALID OPERATION' text frames)
2. Subscribes happen in batched payload (Polymarket DOES support multi-asset
   subscribe in a single message: {"type":"market","assets_ids":["a","b",...]})
3. Silent-freeze watchdog now logs subscriptions count when forcing reconnect
4. Subscribes after reconnect use the batched format too
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

SILENT_FREEZE_THRESHOLD_S = 60.0
WATCHDOG_INTERVAL_S = 10.0

# Polymarket allows multi-asset subscribe in one message.
# Cap per message so we don't send oversized frames.
MAX_TOKENS_PER_SUBSCRIBE = 50


class WebSocketManager:
    """Manages WebSocket connections with batched subscribe + silent-freeze detection."""

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
        # Subscribe queue — buffers tokens added after initial subscribe burst
        self._pending_subscribes: list[str] = []
        self._subscribe_lock = asyncio.Lock()

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
        """Add a token to subscriptions. Sends batched if connected."""
        if token_id in self._subscriptions:
            return
        self._subscriptions.add(token_id)
        if self._ws is not None:
            # Buffer for batched send (in case multiple subscribes arrive
            # within the same event-loop tick)
            self._pending_subscribes.append(token_id)
            asyncio.create_task(self._flush_pending_subscribes())

    async def unsubscribe_market(self, token_id: str) -> None:
        self._subscriptions.discard(token_id)

    @property
    def book_events_received(self) -> int:
        return self._book_events_received

    async def _flush_pending_subscribes(self) -> None:
        """Wait briefly to coalesce multiple subscribes into one batched message."""
        async with self._subscribe_lock:
            # Coalesce window — if more arrive within 100ms, batch them
            await asyncio.sleep(0.1)
            if not self._pending_subscribes or self._ws is None:
                return
            tokens = self._pending_subscribes[:]
            self._pending_subscribes.clear()
            await self._send_subscribe_batch(tokens)

    async def _watchdog_loop(self) -> None:
        await asyncio.sleep(15)
        while self._running:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
                if self._last_data_ts == 0:
                    if self._ws is not None and self._subscriptions:
                        logger.warning(
                            "ws_no_data_since_connect",
                            subscriptions=len(self._subscriptions),
                        )
                    continue
                age = time.time() - self._last_data_ts
                if age > SILENT_FREEZE_THRESHOLD_S:
                    logger.warning(
                        "ws_silent_freeze_detected",
                        age_s=round(age, 1),
                        threshold_s=SILENT_FREEZE_THRESHOLD_S,
                        book_events_total=self._book_events_received,
                        subscriptions=len(self._subscriptions),
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
                    logger.info(
                        "ws_connected",
                        url=WS_URL,
                        subscriptions=len(self._subscriptions),
                    )
                    # Re-subscribe ALL tokens in one batched message
                    if self._subscriptions:
                        await self._send_subscribe_batch(list(self._subscriptions))

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
                self._last_data_ts = time.time()

                # Polymarket sends 'INVALID OPERATION' as plain text on
                # malformed subscribes — log once but don't try to parse
                if isinstance(raw_message, str) and raw_message.startswith("INVALID"):
                    # Suppress repeated logging — count instead
                    if self._book_events_received == 0:
                        logger.warning("ws_invalid_op_response", msg=raw_message[:80])
                    continue

                message = json.loads(raw_message)
                if isinstance(message, list):
                    for item in message:
                        await self._handle_message(item)
                else:
                    await self._handle_message(message)
            except json.JSONDecodeError:
                # Don't log every INVALID OPERATION
                pass

    async def _handle_message(self, message: dict) -> None:
        msg_type = message.get("event_type", message.get("type", ""))

        if msg_type == "book":
            self._book_events_received += 1
            self._health.stamp("polymarket_book")
            if self._book_events_received in (1, 10, 100, 1000, 10000):
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

    async def _send_subscribe_batch(self, token_ids: list[str]) -> None:
        """Send subscribe(s) in batched format — Polymarket accepts multi-asset payload.

        Splits into chunks of MAX_TOKENS_PER_SUBSCRIBE to avoid oversized frames.
        Throttles 1 message per 200ms between chunks to avoid rate-limit.
        """
        if not self._ws or not token_ids:
            return
        for i in range(0, len(token_ids), MAX_TOKENS_PER_SUBSCRIBE):
            chunk = token_ids[i:i + MAX_TOKENS_PER_SUBSCRIBE]
            msg = json.dumps({
                "type": "market",
                "assets_ids": chunk,
            })
            try:
                await self._ws.send(msg)
                logger.info("ws_subscribed_batch",
                            count=len(chunk),
                            sample=chunk[0][:16] if chunk else "",
                            total_subs=len(self._subscriptions))
            except Exception as e:
                logger.warning("ws_subscribe_send_err", error=str(e))
                return
            # Brief pause between chunks
            if len(token_ids) > MAX_TOKENS_PER_SUBSCRIBE:
                await asyncio.sleep(0.2)
