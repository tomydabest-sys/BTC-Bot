"""Data ingestion, normalization, and derived metrics.

DataPipeline wires itself to the EventBus during start(): it consumes
`orderbook_update` / `trade_update` raw frames coming off the WebSocket,
converts them into typed OrderBook / Trade objects, and stores them in
per-market ring buffers. Snapshots built from those buffers feed the
strategies through `get_snapshot`.

Adds a Polymarket mid-price ring buffer per market so strategies can observe
short-window Polymarket movement (poly_move_5s, poly_move_30s) without
needing to keep their own state.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from datetime import datetime, timedelta

import structlog

from polybot.data.models import (
    Market,
    MarketSnapshot,
    OrderBook,
    PriceLevel,
    Side,
    Trade,
)

logger = structlog.get_logger()


class MarketDataBuffer:
    """In-memory ring buffer for a single market's data."""

    def __init__(
        self,
        max_trades: int = 1000,
        max_prices: int = 500,
        max_mid_samples: int = 600,
    ) -> None:
        self.trades: deque[Trade] = deque(maxlen=max_trades)
        self.price_history: deque[float] = deque(maxlen=max_prices)
        self.mid_history: deque[tuple[float, float]] = deque(maxlen=max_mid_samples)
        self.orderbook: OrderBook | None = None
        self._last_mid_ts: float = 0.0

    def add_trade(self, trade: Trade) -> None:
        self.trades.append(trade)
        self.price_history.append(trade.price)

    def update_orderbook(self, orderbook: OrderBook) -> None:
        self.orderbook = orderbook
        now = time.time()
        if now - self._last_mid_ts >= 0.25:
            try:
                mid = float(orderbook.mid_price)
                if 0.0 < mid < 1.0:
                    self.mid_history.append((now, mid))
                    self._last_mid_ts = now
            except Exception:
                pass

    def poly_move_over(self, seconds: float) -> float:
        """Polymarket mid change over last N seconds. 0.0 if insufficient data."""
        if len(self.mid_history) < 2:
            return 0.0
        now = time.time()
        target = now - seconds
        latest_mid = self.mid_history[-1][1]
        old_mid: float | None = None
        for ts, mid in reversed(self.mid_history):
            if ts <= target:
                old_mid = mid
                break
        if old_mid is None:
            if (now - self.mid_history[0][0]) < seconds * 0.5:
                return 0.0
            old_mid = self.mid_history[0][1]
        return latest_mid - old_mid


class DataPipeline:
    """Ingests and serves normalized market data.

    Constructor accepts (client, ws, event_bus) so the orchestrator can wire
    the pipeline once and let it self-subscribe to market data events on
    start(). All three are optional to keep tests and any existing
    construct-then-feed flows working.
    """

    def __init__(
        self,
        client=None,
        ws=None,
        event_bus=None,
    ) -> None:
        self._client = client
        self._ws = ws
        self._event_bus = event_bus
        self._buffers: dict[str, MarketDataBuffer] = {}
        self._markets: dict[str, Market] = {}
        # Resolve per-token book frames back to the market they belong to.
        self._token_to_market: dict[str, str] = {}
        self._started = False

    # ─────────────────────────────────────────────────────────────────
    #  Lifecycle
    # ─────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Subscribe to data events from the bus."""
        if self._started:
            return
        if self._event_bus is not None:
            self._event_bus.subscribe("orderbook_update", self._on_orderbook_update)
            self._event_bus.subscribe("trade_update", self._on_trade_update)
        self._started = True
        logger.info("data_pipeline_started")

    async def stop(self) -> None:
        if not self._started:
            return
        if self._event_bus is not None:
            try:
                self._event_bus.unsubscribe("orderbook_update", self._on_orderbook_update)
                self._event_bus.unsubscribe("trade_update", self._on_trade_update)
            except (ValueError, KeyError):
                pass
        self._started = False
        logger.info("data_pipeline_stopped")

    # ─────────────────────────────────────────────────────────────────
    #  Market registration
    # ─────────────────────────────────────────────────────────────────

    def register_market(self, market: Market) -> None:
        self._markets[market.id] = market
        if market.id not in self._buffers:
            self._buffers[market.id] = MarketDataBuffer()
        for token_id in market.token_ids:
            self._token_to_market[token_id] = market.id

    def unregister_market(self, market_id: str) -> None:
        market = self._markets.pop(market_id, None)
        self._buffers.pop(market_id, None)
        if market is not None:
            for token_id in market.token_ids:
                self._token_to_market.pop(token_id, None)

    def get_market(self, market_id: str) -> Market | None:
        return self._markets.get(market_id)

    # ─────────────────────────────────────────────────────────────────
    #  Manual ingestion (also used by event handlers)
    # ─────────────────────────────────────────────────────────────────

    def ingest_orderbook(self, market_id: str, orderbook: OrderBook) -> None:
        buf = self._buffers.get(market_id)
        if buf:
            buf.update_orderbook(orderbook)

    def ingest_trade(self, market_id: str, trade: Trade) -> None:
        buf = self._buffers.get(market_id)
        if buf:
            buf.add_trade(trade)

    # ─────────────────────────────────────────────────────────────────
    #  Event handlers — convert raw Polymarket frames to typed objects
    # ─────────────────────────────────────────────────────────────────

    async def _on_orderbook_update(self, data: dict, **_) -> None:
        if not isinstance(data, dict):
            return
        token_id = str(data.get("asset_id") or data.get("market") or "")
        market_id = self._token_to_market.get(token_id)
        if not market_id:
            return
        try:
            bids = [
                PriceLevel(float(b["price"]), float(b["size"]))
                for b in data.get("bids", [])
                if "price" in b and "size" in b
            ]
            asks = [
                PriceLevel(float(a["price"]), float(a["size"]))
                for a in data.get("asks", [])
                if "price" in a and "size" in a
            ]
        except (TypeError, ValueError, KeyError) as e:
            logger.debug("orderbook_parse_err", error=str(e))
            return
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        ob = OrderBook(
            market_id=token_id,
            timestamp=datetime.utcnow(),
            bids=bids,
            asks=asks,
        )
        self.ingest_orderbook(market_id, ob)

    async def _on_trade_update(self, data: dict, **_) -> None:
        if not isinstance(data, dict):
            return
        token_id = str(data.get("asset_id") or data.get("market") or "")
        market_id = self._token_to_market.get(token_id)
        if not market_id:
            return
        price_raw = data.get("price")
        size_raw = data.get("size", 0.0)
        if price_raw is None:
            return
        try:
            price = float(price_raw)
            size = float(size_raw)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        side_raw = str(data.get("side", "BUY")).upper()
        side = Side.BUY if side_raw not in ("SELL",) else Side.SELL
        trade = Trade(
            market_id=token_id,
            timestamp=datetime.utcnow(),
            side=side,
            price=price,
            size=size,
            outcome=str(data.get("outcome", "")),
        )
        self.ingest_trade(market_id, trade)

    # ─────────────────────────────────────────────────────────────────
    #  Snapshot
    # ─────────────────────────────────────────────────────────────────

    def get_snapshot(self, market_id: str) -> MarketSnapshot | None:
        market = self._markets.get(market_id)
        buf = self._buffers.get(market_id)
        if not market or not buf or not buf.orderbook:
            return None

        now = datetime.utcnow()
        trades_1h = [t for t in buf.trades if t.timestamp > now - timedelta(hours=1)]
        trades_24h = list(buf.trades)

        return MarketSnapshot(
            market=market,
            orderbook=buf.orderbook,
            recent_trades=trades_1h,
            vwap_1h=self._compute_vwap(trades_1h),
            vwap_24h=self._compute_vwap(trades_24h),
            volatility_1h=self._compute_volatility(trades_1h),
            price_history=list(buf.price_history),
            poly_move_5s=buf.poly_move_over(5.0),
            poly_move_30s=buf.poly_move_over(30.0),
        )

    @staticmethod
    def _compute_vwap(trades: list[Trade]) -> float:
        if not trades:
            return 0.0
        total_volume = sum(t.size for t in trades)
        if total_volume == 0:
            return 0.0
        return sum(t.price * t.size for t in trades) / total_volume

    @staticmethod
    def _compute_volatility(trades: list[Trade]) -> float:
        if len(trades) < 2:
            return 0.0
        prices = [t.price for t in trades]
        return statistics.stdev(prices)
