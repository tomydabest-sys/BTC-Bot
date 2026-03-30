"""Real-time crypto price feeds from major exchanges for reference pricing."""

from __future__ import annotations

import asyncio
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

    ticks: deque[ExchangeTick] = field(default_factory=lambda: deque(maxlen=2000))
    last_price: float = 0.0
    last_update: float = 0.0

    def add_tick(self, tick: ExchangeTick) -> None:
        self.ticks.append(tick)
        self.last_price = tick.price
        self.last_update = tick.timestamp

    @property
    def price_1s_ago(self) -> float:
        """Price approximately 1 second ago."""
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
        now = time.time()
        old_price = 0.0
        for tick in self.ticks:
            if now - tick.timestamp >= seconds:
                old_price = tick.price
                break
        if old_price == 0:
            return 0.0
        return (self.last_price - old_price) / old_price

    def momentum_score(self) -> float:
        """Composite momentum: weighted recent price changes.

        Positive = moving up, negative = moving down.
        Magnitude indicates strength.
        """
        changes = []
        windows = [5, 15, 30, 60]
        weights = [0.4, 0.3, 0.2, 0.1]
        for w, weight in zip(windows, weights):
            pct = self.price_change_pct(w)
            changes.append(pct * weight)
        return sum(changes)


class ExchangePriceFeed:
    """Aggregates real-time prices from Binance and Coinbase."""

    BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
    COINBASE_TICKER_URL = "https://api.coinbase.com/v2/prices/{pair}/spot"

    # Map common names to exchange symbols
    SYMBOL_MAP = {
        "BTC": {"binance": "BTCUSDT", "coinbase": "BTC-USD"},
        "ETH": {"binance": "ETHUSDT", "coinbase": "ETH-USD"},
        "SOL": {"binance": "SOLUSDT", "coinbase": "SOL-USD"},
        "XRP": {"binance": "XRPUSDT", "coinbase": "XRP-USD"},
        "MATIC": {"binance": "MATICUSDT", "coinbase": "MATIC-USD"},
        "DOGE": {"binance": "DOGEUSDT", "coinbase": "DOGE-USD"},
    }

    def __init__(self, symbols: list[str] | None = None, poll_interval: float = 0.5) -> None:
        self._symbols = symbols or ["BTC", "ETH", "SOL", "XRP"]
        self._poll_interval = poll_interval
        self._feeds: dict[str, PriceFeedState] = {s: PriceFeedState() for s in self._symbols}
        self._client: httpx.AsyncClient | None = None
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def feeds(self) -> dict[str, PriceFeedState]:
        return self._feeds

    def get_price(self, symbol: str) -> float:
        feed = self._feeds.get(symbol.upper())
        return feed.last_price if feed else 0.0

    def get_feed(self, symbol: str) -> PriceFeedState | None:
        return self._feeds.get(symbol.upper())

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=5.0)
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("exchange_feed_started", symbols=self._symbols)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        if self._client:
            await self._client.aclose()

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._fetch_binance_batch()
            except Exception as e:
                logger.debug("binance_fetch_error", error=str(e))
            try:
                await self._fetch_coinbase_batch()
            except Exception as e:
                logger.debug("coinbase_fetch_error", error=str(e))
            await asyncio.sleep(self._poll_interval)

    async def _fetch_binance_batch(self) -> None:
        assert self._client
        binance_symbols = [
            self.SYMBOL_MAP[s]["binance"]
            for s in self._symbols
            if s in self.SYMBOL_MAP
        ]
        # Binance supports fetching multiple symbols at once
        for sym_key in self._symbols:
            if sym_key not in self.SYMBOL_MAP:
                continue
            bsym = self.SYMBOL_MAP[sym_key]["binance"]
            try:
                resp = await self._client.get(
                    self.BINANCE_TICKER_URL, params={"symbol": bsym}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    bid = float(data.get("bidPrice", 0))
                    ask = float(data.get("askPrice", 0))
                    mid = (bid + ask) / 2 if bid and ask else 0
                    if mid > 0:
                        tick = ExchangeTick(
                            symbol=sym_key,
                            price=mid,
                            timestamp=time.time(),
                            source="binance",
                            bid=bid,
                            ask=ask,
                        )
                        self._feeds[sym_key].add_tick(tick)
            except Exception:
                pass

    async def _fetch_coinbase_batch(self) -> None:
        assert self._client
        for sym_key in self._symbols:
            if sym_key not in self.SYMBOL_MAP:
                continue
            pair = self.SYMBOL_MAP[sym_key]["coinbase"]
            try:
                url = self.COINBASE_TICKER_URL.format(pair=pair)
                resp = await self._client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    price = float(data.get("data", {}).get("amount", 0))
                    if price > 0:
                        tick = ExchangeTick(
                            symbol=sym_key,
                            price=price,
                            timestamp=time.time(),
                            source="coinbase",
                        )
                        self._feeds[sym_key].add_tick(tick)
            except Exception:
                pass
