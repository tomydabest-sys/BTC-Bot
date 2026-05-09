"""BTC spot price feed — Binance WS with REST fallback and a mock mode.

Multiple Binance endpoints are tried in order; falls back to REST polling on
sustained WS failure. Set BTC_BOT_USE_MOCK_FEED=1 (or pass use_mock=True) to
substitute a deterministic synthetic feed for offline testing.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import Iterable

import httpx
import structlog
import websockets
from websockets.exceptions import ConnectionClosed

from polybot.health_monitor import get_monitor

logger = structlog.get_logger()


BINANCE_WS_ENDPOINTS = [
    "wss://stream.binance.com:9443/ws/btcusdt@trade",
    "wss://stream.binance.com:443/ws/btcusdt@trade",
    "wss://data-stream.binance.com:9443/ws/btcusdt@trade",
]
BINANCE_REST_PRICE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

MAX_TICK_HISTORY = 4096


@dataclass
class _Tick:
    price: float
    timestamp: float


class PriceFeedState:
    """Bounded in-memory tick buffer with derived helpers used by strategies."""

    def __init__(self, max_history: int = MAX_TICK_HISTORY) -> None:
        self.ticks: deque[_Tick] = deque(maxlen=max_history)
        self.last_price: float = 0.0

    def push(self, price: float, ts: float | None = None) -> None:
        if price <= 0:
            return
        ts = ts if ts is not None else time.time()
        self.ticks.append(_Tick(price=price, timestamp=ts))
        self.last_price = price

    def price_change_since(self, seconds_ago: float) -> float:
        """Fractional change from the price `seconds_ago` ago to the latest tick."""
        if not self.ticks or self.last_price <= 0:
            return 0.0
        cutoff = time.time() - seconds_ago
        # Walk from oldest forward — return the first tick at-or-after cutoff
        ref = None
        for t in self.ticks:
            if t.timestamp >= cutoff:
                ref = t
                break
        if ref is None or ref.price <= 0:
            return 0.0
        return (self.last_price - ref.price) / ref.price


class ExchangeFeed:
    """Binance spot WS feed with REST fallback. Symbol assumed BTC."""

    def __init__(
        self,
        symbol: str = "BTC",
        use_mock: bool | None = None,
    ) -> None:
        self._symbol = symbol.upper()
        self._state = PriceFeedState()
        self._running = False
        self._ws_task: asyncio.Task | None = None
        self._rest_task: asyncio.Task | None = None
        self._mock_task: asyncio.Task | None = None
        self._consecutive_ws_failures = 0
        self._last_ws_msg_ts = 0.0
        self._health = get_monitor()

        # Resolve mock flag
        if use_mock is None:
            use_mock = os.environ.get("BTC_BOT_USE_MOCK_FEED") == "1"
        self._use_mock = bool(use_mock)

    @property
    def state(self) -> PriceFeedState:
        return self._state

    @property
    def use_mock(self) -> bool:
        return self._use_mock

    # ─────────────────────────────────────────────────────────────────
    #  Dashboard-facing surface
    # ─────────────────────────────────────────────────────────────────

    @property
    def feeds(self) -> dict[str, "ExchangeFeed"]:
        """Symbol → feed mapping. Single-symbol today; reserved for
        multi-symbol expansion. The dashboard iterates this dict."""
        return {self._symbol: self}

    @property
    def last_price(self) -> float:
        return self._state.last_price

    @property
    def last_update(self) -> float:
        if self._state.ticks:
            return self._state.ticks[-1].timestamp
        return 0.0

    def price_change_pct(self, seconds: float) -> float:
        """Fractional change over the last `seconds` (e.g. 0.005 = +0.5%)."""
        return self._state.price_change_since(seconds)

    def volatility_window(self, seconds: float) -> float:
        """Stdev of prices within the last `seconds`. 0.0 if insufficient."""
        if not self._state.ticks:
            return 0.0
        cutoff = time.time() - seconds
        prices = [t.price for t in self._state.ticks if t.timestamp >= cutoff]
        if len(prices) < 2:
            return 0.0
        try:
            import statistics
            return statistics.stdev(prices)
        except statistics.StatisticsError:
            return 0.0

    def momentum_score(self) -> float:
        """Simple momentum: ratio of short-window change to long-window
        change, signed. Returns 0.0 when either window is empty."""
        short = self.price_change_pct(15)
        long_ = self.price_change_pct(60)
        if long_ == 0:
            return short
        return short - long_

    async def start(self) -> None:
        self._running = True
        if self._use_mock:
            from polybot.data.mock_btc_feed import MockBTCFeed
            mock = MockBTCFeed()
            self._mock_task = asyncio.create_task(mock.run(self._state))
            logger.info("exchange_feed_mock_started", symbol=self._symbol)
            return
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._rest_task = asyncio.create_task(self._rest_fallback_loop())

    async def stop(self) -> None:
        self._running = False
        for t in (self._ws_task, self._rest_task, self._mock_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    # ─────────────────────────────────────────────────────────────────
    #  WS path
    # ─────────────────────────────────────────────────────────────────

    async def _ws_loop(self) -> None:
        idx = 0
        backoff = 1.0
        while self._running:
            url = BINANCE_WS_ENDPOINTS[idx % len(BINANCE_WS_ENDPOINTS)]
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, close_timeout=5
                ) as ws:
                    logger.info("binance_ws_connected", url=url)
                    backoff = 1.0
                    self._consecutive_ws_failures = 0
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            price = float(msg.get("p", 0))
                            if price > 0:
                                self._state.push(price)
                                self._last_ws_msg_ts = time.time()
                                self._health.stamp("binance_btc")
                        except (json.JSONDecodeError, ValueError, TypeError):
                            continue
            except ConnectionClosed:
                self._consecutive_ws_failures += 1
                logger.warning("binance_ws_closed", url=url)
            except Exception as e:
                self._consecutive_ws_failures += 1
                logger.warning("binance_ws_err", url=url, error=str(e)[:100])
            idx += 1
            if self._running:
                await asyncio.sleep(min(backoff, 30.0))
                backoff *= 1.5

    async def _rest_fallback_loop(self) -> None:
        """Poll Binance REST whenever WS hasn't delivered for >30s."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            while self._running:
                await asyncio.sleep(5)
                if (time.time() - self._last_ws_msg_ts) < 30:
                    continue
                try:
                    resp = await client.get(BINANCE_REST_PRICE_URL)
                    if resp.status_code == 200:
                        price = float(resp.json().get("price", 0))
                        if price > 0:
                            self._state.push(price)
                            self._health.stamp("binance_btc")
                            logger.debug("binance_rest_fallback_tick", price=price)
                except Exception as e:
                    logger.debug("binance_rest_err", error=str(e))


# ───────────────────────────────────────────────────────────────────────────
#  CLI for one-off testing
# ───────────────────────────────────────────────────────────────────────────

async def _cli_main() -> None:
    feed = ExchangeFeed(use_mock=os.environ.get("BTC_BOT_USE_MOCK_FEED") == "1")
    await feed.start()
    try:
        while True:
            await asyncio.sleep(2)
            print(
                f"last={feed.state.last_price:.2f}  "
                f"30s_change={feed.state.price_change_since(30):+.4%}"
            )
    finally:
        await feed.stop()


if __name__ == "__main__":
    asyncio.run(_cli_main())
