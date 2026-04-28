"""Real-time crypto price feeds via WebSocket for minimum latency.

PATCHED FROM ORIGINAL:
1. Multi-endpoint fallback for Binance: tries .com → .us → futures
   if connection fails (handles Windows firewall / corporate proxy / geo-block)
2. Logs every connect attempt with exception class for diagnostic clarity
3. REST fallback to Coinbase widened to also poll Binance REST
   if all WS endpoints fail
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field

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
        if len(self.ticks) < 2:
            return 0.0
        window = self.ticks[-1].timestamp - self.ticks[0].timestamp
        if window <= 0:
            return 0.0
        return len(self.ticks) / window

    @property
    def is_stale(self) -> bool:
        if self.last_update == 0:
            return False
        return (time.time() - self.last_update) > 15.0

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
        target = time.time() - (ms / 1000.0)
        for tick in reversed(self.ticks):
            if tick.timestamp <= target:
                return tick.price
        return self.ticks[0].price if self.ticks else 0.0

    def price_change_since(self, seconds_ago: float) -> float:
        now = time.time()
        target = now - seconds_ago
        old_price = 0.0
        for tick in reversed(self.ticks):
            if tick.timestamp <= target:
                old_price = tick.price
                break
        if old_price == 0:
            if self.ticks:
                old_price = self.ticks[0].price
            else:
                return 0.0
        if old_price == 0:
            return 0.0
        return (self.last_price - old_price) / old_price

    def volatility_window(self, seconds: int = 60) -> float:
        now = time.time()
        prices = [t.price for t in self.ticks if now - t.timestamp <= seconds]
        if len(prices) < 2:
            return 0.0
        mean = sum(prices) / len(prices)
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        return variance**0.5

    def price_change_pct(self, seconds: int = 60) -> float:
        return self.price_change_since(float(seconds))

    def momentum_score(self) -> float:
        changes = []
        windows = [5, 15, 30, 60]
        weights = [0.4, 0.3, 0.2, 0.1]
        for w, weight in zip(windows, weights):
            pct = self.price_change_pct(w)
            changes.append(pct * weight)
        return sum(changes)

    def micro_momentum(self) -> float:
        changes = []
        for ms, weight in [(200, 0.4), (500, 0.3), (1000, 0.2), (2000, 0.1)]:
            pct = self.price_change_since(ms / 1000.0)
            changes.append(pct * weight)
        return sum(changes)


# ─────────────────────────────────────────────────────────────────────────────
#  BINANCE WEBSOCKET STREAM (with multi-endpoint fallback)
# ─────────────────────────────────────────────────────────────────────────────


# Endpoints tried in order. .us works in geo-restricted regions; futures works
# even when spot is blocked by some firewalls.
BINANCE_ENDPOINTS = [
    "wss://stream.binance.com:9443/stream",
    "wss://stream.binance.us:9443/stream",
    "wss://fstream.binance.com/stream",
]


class BinanceWebSocketFeed:
    """Connects to Binance bookTicker stream with fallback endpoints."""

    def __init__(self, symbols: list[str], feeds: dict[str, PriceFeedState]) -> None:
        self._symbols = symbols
        self._feeds = feeds
        self._running = False
        self._task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._reconnect_delay = 0.5
        self._force_reconnect = asyncio.Event()
        self._connect_count = 0
        self._endpoint_idx = 0

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
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop(self) -> None:
        self._running = False
        self._force_reconnect.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass

    def _build_stream_url(self) -> str:
        streams = []
        for sym in self._symbols:
            binance_sym = self._reverse_map.get(sym.upper())
            if binance_sym:
                streams.append(f"{binance_sym}@bookTicker")
        if not streams:
            return ""
        endpoint = BINANCE_ENDPOINTS[self._endpoint_idx % len(BINANCE_ENDPOINTS)]
        return f"{endpoint}?streams={'/'.join(streams)}"

    async def _watchdog_loop(self) -> None:
        await asyncio.sleep(20)
        while self._running:
            try:
                await asyncio.sleep(5)
                btc_feed = self._feeds.get("BTC")
                if btc_feed and btc_feed.last_update > 0:
                    age = time.time() - btc_feed.last_update
                    if age > 15:
                        logger.warning(
                            "ws_feed_stale_force_reconnect",
                            seconds_since_last_tick=round(age, 1),
                        )
                        self._force_reconnect.set()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("watchdog_error", error=str(e))

    async def _connection_loop(self) -> None:
        import websockets
        from websockets.exceptions import ConnectionClosed, ConnectionClosedError

        consecutive_failures_per_endpoint: dict[int, int] = {}

        while self._running:
            self._connect_count += 1
            url = self._build_stream_url()
            if not url:
                logger.error("binance_ws_no_symbols")
                return

            current_endpoint = BINANCE_ENDPOINTS[self._endpoint_idx % len(BINANCE_ENDPOINTS)]
            try:
                logger.info(
                    "binance_ws_connecting",
                    endpoint=current_endpoint,
                    attempt=self._connect_count,
                    endpoint_idx=self._endpoint_idx,
                )
                async with websockets.connect(
                    url,
                    ping_interval=10,
                    ping_timeout=10,
                    close_timeout=5,
                    max_queue=2048,
                ) as ws:
                    self._reconnect_delay = 0.5
                    self._force_reconnect.clear()
                    consecutive_failures_per_endpoint[self._endpoint_idx] = 0
                    logger.info(
                        "binance_ws_connected",
                        endpoint=current_endpoint,
                        symbols=self._symbols,
                    )

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

            except (ConnectionClosed, ConnectionClosedError) as e:
                logger.warning("binance_ws_disconnected",
                               endpoint=current_endpoint,
                               code=getattr(e, "code", None))
                consecutive_failures_per_endpoint[self._endpoint_idx] = (
                    consecutive_failures_per_endpoint.get(self._endpoint_idx, 0) + 1
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "binance_ws_error",
                    endpoint=current_endpoint,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                consecutive_failures_per_endpoint[self._endpoint_idx] = (
                    consecutive_failures_per_endpoint.get(self._endpoint_idx, 0) + 1
                )

            # If this endpoint has failed 3 times in a row, rotate to next
            if consecutive_failures_per_endpoint.get(self._endpoint_idx, 0) >= 3:
                old_idx = self._endpoint_idx
                self._endpoint_idx = (self._endpoint_idx + 1) % len(BINANCE_ENDPOINTS)
                logger.warning(
                    "binance_ws_rotating_endpoint",
                    from_endpoint=BINANCE_ENDPOINTS[old_idx],
                    to_endpoint=BINANCE_ENDPOINTS[self._endpoint_idx],
                )
                consecutive_failures_per_endpoint[self._endpoint_idx] = 0

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 1.5, 10.0)

    async def _receive_loop(self, ws) -> None:
        async for raw_msg in ws:
            if not self._running:
                break
            try:
                msg = json.loads(raw_msg)
                data = msg.get("data", msg)
                self._process_tick(data)
            except (json.JSONDecodeError, KeyError):
                continue

    def _process_tick(self, data: dict) -> None:
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


# ─────────────────────────────────────────────────────────────────────────────
#  REST FALLBACK (polls multiple sources at 5 Hz)
# ─────────────────────────────────────────────────────────────────────────────


class RestFallbackFeed:
    """Polls REST endpoints when WS feeds are stale.

    Sources tried in order:
      1. Binance REST: api.binance.com /api/v3/ticker/bookTicker
      2. Binance.us REST: api.binance.us
      3. Coinbase: api.coinbase.com
    """

    BINANCE_REST_URLS = [
        "https://api.binance.com/api/v3/ticker/bookTicker?symbol={pair}",
        "https://api.binance.us/api/v3/ticker/bookTicker?symbol={pair}",
    ]
    COINBASE_URL = "https://api.coinbase.com/v2/prices/{pair}/spot"

    BINANCE_PAIR_MAP = {"BTC": "BTCUSDT", "ETH": "ETHUSDT",
                       "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
    COINBASE_PAIR_MAP = {"BTC": "BTC-USD", "ETH": "ETH-USD",
                        "SOL": "SOL-USD", "XRP": "XRP-USD"}

    def __init__(self, symbols: list[str], feeds: dict[str, PriceFeedState]) -> None:
        self._symbols = symbols
        self._feeds = feeds
        self._client: httpx.AsyncClient | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._working_endpoint_idx: int = 0  # 0=binance.com, 1=binance.us, 2=coinbase

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=5.0)
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client:
            await self._client.aclose()

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                for sym in self._symbols:
                    feed = self._feeds[sym]
                    # Only use REST if WS feed is stale or empty
                    if not feed.is_stale and feed.last_update > 0:
                        continue

                    price = await self._fetch_price(sym)
                    if price > 0:
                        tick = ExchangeTick(
                            symbol=sym, price=price,
                            timestamp=time.time(),
                            source=f"rest_{self._working_endpoint_idx}",
                        )
                        feed.add_tick(tick)
                await asyncio.sleep(0.2)  # 5 Hz polling
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("rest_poll_error", error=str(e))
                await asyncio.sleep(1.0)

    async def _fetch_price(self, sym: str) -> float:
        # Try the last working endpoint first
        for offset in range(3):
            idx = (self._working_endpoint_idx + offset) % 3
            try:
                if idx == 0:
                    pair = self.BINANCE_PAIR_MAP.get(sym)
                    if not pair:
                        continue
                    url = self.BINANCE_REST_URLS[0].format(pair=pair)
                    resp = await self._client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        bid, ask = float(data["bidPrice"]), float(data["askPrice"])
                        self._working_endpoint_idx = idx
                        return (bid + ask) / 2.0
                elif idx == 1:
                    pair = self.BINANCE_PAIR_MAP.get(sym)
                    if not pair:
                        continue
                    url = self.BINANCE_REST_URLS[1].format(pair=pair)
                    resp = await self._client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        bid, ask = float(data["bidPrice"]), float(data["askPrice"])
                        self._working_endpoint_idx = idx
                        return (bid + ask) / 2.0
                elif idx == 2:
                    pair = self.COINBASE_PAIR_MAP.get(sym)
                    if not pair:
                        continue
                    url = self.COINBASE_URL.format(pair=pair)
                    resp = await self._client.get(url)
                    if resp.status_code == 200:
                        price = float(resp.json().get("data", {}).get("amount", 0))
                        if price > 0:
                            self._working_endpoint_idx = idx
                            return price
            except Exception:
                continue
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN FEED AGGREGATOR
# ─────────────────────────────────────────────────────────────────────────────


class ExchangePriceFeed:
    """Aggregates real-time prices from WS + REST fallback."""

    def __init__(self, symbols: list[str] | None = None, poll_interval: float = 0.5) -> None:
        self._symbols = symbols or ["BTC", "ETH", "SOL", "XRP"]
        self._poll_interval = poll_interval
        self._feeds: dict[str, PriceFeedState] = {s: PriceFeedState() for s in self._symbols}

        self._binance_ws = BinanceWebSocketFeed(self._symbols, self._feeds)
        self._rest_fallback = RestFallbackFeed(self._symbols, self._feeds)

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
        await self._rest_fallback.start()
        logger.info(
            "exchange_feed_started",
            symbols=self._symbols,
            primary="binance_ws_with_fallback",
            secondary="rest_5hz",
        )

    async def stop(self) -> None:
        await self._binance_ws.stop()
        await self._rest_fallback.stop()
