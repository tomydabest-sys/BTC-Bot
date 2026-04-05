"""Real-time crypto price feeds via WebSocket for minimum latency.

Binance WebSocket stream delivers price ticks in ~50ms vs 500ms+ for REST.
This is the single biggest speed improvement for latency arb — cutting
the exchange data lag from 500ms to ~50ms.

Uses Binance's individual bookTicker stream (best bid/ask updates)
which fires on EVERY orderbook change, not just on a timer.
Coinbase REST is kept as fallback only.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

import httpx
import structlog

logger = structlog.get_logger()


@dataclass
class ExchangeTick:
    symbol: str
    price: float
    timestamp: float
    source: str
    volume_24h: float = 0.0
    bid: float = 0.0
    ask: float = 0.0


@dataclass
class PriceFeedState:
    """Tracks price history and computes derived signals for a symbol."""

    ticks: deque[ExchangeTick] = field(default_factory=lambda: deque(maxlen=5000))
    last_price: float = 0.0
    last_update: float = 0.0
    _tick_count: int = 0

    def add_tick(self, tick: ExchangeTick) -> None:
        self.ticks.append(tick)
        self.last_price = tick.price
        self.last_update = tick.timestamp
        self._tick_count += 1

    @property
    def ticks_per_second(self) -> float:
        """Measure feed speed — should be 5-20 tps on Binance WS."""
        if len(self.ticks) < 2:
            return 0.0
        window = self.ticks[-1].timestamp - self.ticks[0].timestamp
        if window <= 0:
            return 0.0
        return len(self.ticks) / window

    @property
    def price_1s_ago(self) -> float:
        now = time.time()
        for tick in reversed(self.ticks):
            if now - tick.timestamp >= 1.0:
                return tick.price
        return self.ticks[0].price if self.ticks else 0.0

    @property
    def price_5s_ago(self) -> float:
        now = time.time()
        for tick in reversed(self.ticks):
            if now - tick.timestamp >= 5.0:
                return tick.price
        return self.ticks[0].price if self.ticks else 0.0

    @property
    def price_30s_ago(self) -> float:
        now = time.time()
        for tick in reversed(self.ticks):
            if now - tick.timestamp >= 30.0:
                return tick.price
        return self.ticks[0].price if self.ticks else 0.0

    @property
    def price_60s_ago(self) -> float:
        now = time.time()
        for tick in reversed(self.ticks):
            if now - tick.timestamp >= 60.0:
                return tick.price
        return self.ticks[0].price if self.ticks else 0.0

    def price_n_ms_ago(self, ms: int) -> float:
        """Get price from N milliseconds ago — for sub-second arb detection."""
        target = time.time() - (ms / 1000.0)
        for tick in reversed(self.ticks):
            if tick.timestamp <= target:
                return tick.price
        return self.ticks[0].price if self.ticks else 0.0

    def price_change_since(self, seconds_ago: float) -> float:
        """Price change % over the last N seconds. Works with fractional seconds."""
        now = time.time()
        target = now - seconds_ago
        old_price = 0.0
        for tick in reversed(self.ticks):
            if tick.timestamp <= target:
                old_price = tick.price
                break
        if old_price == 0:
            # Fallback to oldest available tick
            if self.ticks:
                old_price = self.ticks[0].price
            else:
                return 0.0
        if old_price == 0:
            return 0.0
        return (self.last_price - old_price) / old_price

    def volatility_window(self, seconds: int = 60) -> float:
        """Standard deviation of prices over a time window."""
        now = time.time()
        prices = [t.price for t in self.ticks if now - t.timestamp <= seconds]
        if len(prices) < 2:
            return 0.0
        mean = sum(prices) / len(prices)
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        return variance**0.5

    def price_change_pct(self, seconds: int = 60) -> float:
        """Percentage price change over window."""
        return self.price_change_since(float(seconds))

    def momentum_score(self) -> float:
        """Composite momentum: weighted recent price changes."""
        changes = []
        windows = [5, 15, 30, 60]
        weights = [0.4, 0.3, 0.2, 0.1]
        for w, weight in zip(windows, weights):
            pct = self.price_change_pct(w)
            changes.append(pct * weight)
        return sum(changes)

    def micro_momentum(self) -> float:
        """Sub-second momentum for latency arb — uses last 2s of ticks.

        Returns the direction and strength of the very latest price movement.
        Positive = price ticking up, negative = ticking down.
        Magnitude = how fast.
        """
        changes = []
        # Weight: 200ms most, 500ms, 1s, 2s least
        for ms, weight in [(200, 0.4), (500, 0.3), (1000, 0.2), (2000, 0.1)]:
            pct = self.price_change_since(ms / 1000.0)
            changes.append(pct * weight)
        return sum(changes)


# ═══════════════════════════════════════════════════════════════
#  BINANCE WEBSOCKET STREAM
# ═══════════════════════════════════════════════════════════════

BINANCE_WS_BASE = "wss://stream.binance.com:9443/ws"


class BinanceWebSocketFeed:
    """Connects to Binance bookTicker stream for real-time best bid/ask.

    bookTicker fires on EVERY orderbook top-of-book change — typically
    5-20 updates per second for BTC. This gives ~50ms latency vs
    500ms+ for REST polling.
    """

    def __init__(self, symbols: list[str], feeds: dict[str, PriceFeedState]) -> None:
        self._symbols = symbols
        self._feeds = feeds
        self._running = False
        self._task: asyncio.Task | None = None
        self._reconnect_delay = 1.0

        # Map Binance symbols to our internal names
        self._symbol_map = {
            "btcusdt": "BTC",
            "ethusdt": "ETH",
            "solusdt": "SOL",
            "xrpusdt": "XRP",
            "maticusdt": "MATIC",
            "dogeusdt": "DOGE",
        }
        self._reverse_map = {v: k for k, v in self._symbol_map.items()}

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._connection_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    def _build_stream_url(self) -> str:
        """Build combined stream URL for all symbols."""
        streams = []
        for sym in self._symbols:
            binance_sym = self._reverse_map.get(sym.upper())
            if binance_sym:
                streams.append(f"{binance_sym}@bookTicker")
        if not streams:
            return ""
        return f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

    async def _connection_loop(self) -> None:
        import websockets
        from websockets.exceptions import ConnectionClosed

        url = self._build_stream_url()
        if not url:
            logger.error("binance_ws_no_symbols")
            return

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    self._reconnect_delay = 1.0
                    logger.info("binance_ws_connected", symbols=self._symbols)

                    async for raw_msg in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw_msg)
                            data = msg.get("data", msg)
                            self._process_tick(data)
                        except (json.JSONDecodeError, KeyError):
                            continue

            except ConnectionClosed as e:
                logger.warning("binance_ws_disconnected", code=e.code)
            except Exception as e:
                logger.error("binance_ws_error", error=str(e))

            if self._running:
                logger.info("binance_ws_reconnecting", delay=self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    def _process_tick(self, data: dict) -> None:
        """Process a bookTicker update. Called for every top-of-book change."""
        raw_symbol = data.get("s", "").lower()
        sym = self._symbol_map.get(raw_symbol)
        if not sym or sym not in self._feeds:
            return

        bid = float(data.get("b", 0))
        ask = float(data.get("a", 0))
        mid = (bid + ask) / 2.0
        if mid <= 0:
            return

        tick = ExchangeTick(
            symbol=sym,
            price=mid,
            timestamp=time.time(),
            source="binance_ws",
            bid=bid,
            ask=ask,
        )
        self._feeds[sym].add_tick(tick)


# ═══════════════════════════════════════════════════════════════
#  COINBASE REST FALLBACK (kept for redundancy)
# ═══════════════════════════════════════════════════════════════

class CoinbaseRestFallback:
    """Polls Coinbase every 2s as a backup price source."""

    COINBASE_URL = "https://api.coinbase.com/v2/prices/{pair}/spot"
    SYMBOL_MAP = {
        "BTC": "BTC-USD", "ETH": "ETH-USD",
        "SOL": "SOL-USD", "XRP": "XRP-USD",
    }

    def __init__(self, symbols: list[str], feeds: dict[str, PriceFeedState]) -> None:
        self._symbols = symbols
        self._feeds = feeds
        self._client: httpx.AsyncClient | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=5.0)
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        if self._client:
            await self._client.aclose()

    async def _poll_loop(self) -> None:
        while self._running:
            for sym in self._symbols:
                pair = self.SYMBOL_MAP.get(sym)
                if not pair:
                    continue
                try:
                    url = self.COINBASE_URL.format(pair=pair)
                    resp = await self._client.get(url)
                    if resp.status_code == 200:
                        price = float(resp.json().get("data", {}).get("amount", 0))
                        if price > 0:
                            tick = ExchangeTick(
                                symbol=sym, price=price,
                                timestamp=time.time(), source="coinbase",
                            )
                            self._feeds[sym].add_tick(tick)
                except Exception:
                    pass
            await asyncio.sleep(2.0)


# ═══════════════════════════════════════════════════════════════
#  MAIN FEED AGGREGATOR
# ═══════════════════════════════════════════════════════════════

class ExchangePriceFeed:
    """Aggregates real-time prices. Primary: Binance WS. Fallback: Coinbase REST."""

    def __init__(self, symbols: list[str] | None = None, poll_interval: float = 0.5) -> None:
        self._symbols = symbols or ["BTC", "ETH", "SOL", "XRP"]
        self._poll_interval = poll_interval
        self._feeds: dict[str, PriceFeedState] = {s: PriceFeedState() for s in self._symbols}

        self._binance_ws = BinanceWebSocketFeed(self._symbols, self._feeds)
        self._coinbase_rest = CoinbaseRestFallback(self._symbols, self._feeds)

    @property
    def feeds(self) -> dict[str, PriceFeedState]:
        return self._feeds

    def get_price(self, symbol: str) -> float:
        feed = self._feeds.get(symbol.upper())
        return feed.last_price if feed else 0.0

    def get_feed(self, symbol: str) -> PriceFeedState | None:
        return self._feeds.get(symbol.upper())

    async def start(self) -> None:
        await self._binance_ws.start()
        await self._coinbase_rest.start()
        logger.info(
            "exchange_feed_started",
            symbols=self._symbols,
            primary="binance_ws",
            fallback="coinbase_rest",
        )

    async def stop(self) -> None:
        await self._binance_ws.stop()
        await self._coinbase_rest.stop()
