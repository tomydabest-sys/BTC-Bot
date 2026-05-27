"""Polymarket WS orderbook subscriber + MockClient with fault injection.

V2 subscribe spec; exponential backoff on disconnect: 1, 2, 4, 8, max 30s.
Every reconnect is logged to the decision log so the operator can audit.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

V2_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONNECT_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


@dataclass
class BookUpdate:
    token_id: str
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    timestamp_ms: int


class WebsocketOrderbook:
    """Live WS subscriber. Uses ``websockets`` package."""

    def __init__(self, token_ids: list[str], url: str = V2_WS_URL) -> None:
        self._token_ids = list(token_ids)
        self._url = url
        self._stop = asyncio.Event()
        self._on_disconnect: Callable[[int, str], None] | None = None

    def set_disconnect_callback(self, fn: Callable[[int, str], None]) -> None:
        self._on_disconnect = fn

    async def stream(self) -> AsyncIterator[BookUpdate]:
        import websockets

        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self._url) as ws:
                    sub = json.dumps({"type": "subscribe", "assets_ids": self._token_ids})
                    await ws.send(sub)
                    attempt = 0
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("event") == "INVALID_OPERATION":
                            logger.error("ws_invalid_operation", raw=raw)
                            continue
                        if msg.get("event") != "book":
                            continue
                        yield BookUpdate(
                            token_id=msg["asset_id"],
                            bids=[(float(b["price"]), float(b["size"])) for b in msg.get("bids", [])],
                            asks=[(float(a["price"]), float(a["size"])) for a in msg.get("asks", [])],
                            timestamp_ms=int(msg.get("timestamp", 0)),
                        )
            except Exception as exc:  # noqa: BLE001
                backoff = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                logger.warning("ws_reconnect", attempt=attempt, backoff_s=backoff, error=str(exc))
                if self._on_disconnect:
                    self._on_disconnect(attempt, str(exc))
                attempt += 1
                await asyncio.sleep(backoff)

    async def stop(self) -> None:
        self._stop.set()


class MockWebsocketOrderbook:
    """Cycles through synthetic book updates + optional fault injection."""

    def __init__(
        self,
        token_ids: list[str],
        fault_after: int | None = None,
        seed_books: dict[str, BookUpdate] | None = None,
    ) -> None:
        self._token_ids = list(token_ids)
        self._fault_after = fault_after
        self._seed_books = seed_books or {}
        self._stop = asyncio.Event()

    async def stream(self) -> AsyncIterator[BookUpdate]:
        emitted = 0
        attempt = 0
        while not self._stop.is_set():
            for token_id in self._token_ids:
                if self._stop.is_set():
                    return
                book = self._seed_books.get(token_id)
                if book is None:
                    book = BookUpdate(
                        token_id=token_id,
                        bids=[(0.40, 100), (0.39, 200)],
                        asks=[(0.42, 100), (0.43, 200)],
                        timestamp_ms=0,
                    )
                yield book
                emitted += 1
                if self._fault_after is not None and emitted >= self._fault_after:
                    backoff = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                    logger.info("ws_mock_inject_fault", backoff_s=backoff)
                    attempt += 1
                    self._fault_after = None
                    await asyncio.sleep(0)  # cooperative pause; no real sleep in tests
            await asyncio.sleep(0)
            return

    async def stop(self) -> None:
        self._stop.set()
