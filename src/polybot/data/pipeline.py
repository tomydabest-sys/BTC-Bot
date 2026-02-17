"""Data ingestion, normalization, and derived metrics."""

from __future__ import annotations

import statistics
from collections import deque
from datetime import datetime, timedelta

from polybot.data.models import MarketSnapshot, OrderBook, Trade, Market


class MarketDataBuffer:
    """In-memory ring buffer for a single market's data."""

    def __init__(self, max_trades: int = 1000, max_prices: int = 500) -> None:
        self.trades: deque[Trade] = deque(maxlen=max_trades)
        self.price_history: deque[float] = deque(maxlen=max_prices)
        self.orderbook: OrderBook | None = None

    def add_trade(self, trade: Trade) -> None:
        self.trades.append(trade)
        self.price_history.append(trade.price)

    def update_orderbook(self, orderbook: OrderBook) -> None:
        self.orderbook = orderbook


class DataPipeline:
    """Ingests and serves normalized market data."""

    def __init__(self) -> None:
        self._buffers: dict[str, MarketDataBuffer] = {}
        self._markets: dict[str, Market] = {}

    def register_market(self, market: Market) -> None:
        self._markets[market.id] = market
        if market.id not in self._buffers:
            self._buffers[market.id] = MarketDataBuffer()

    def ingest_orderbook(self, market_id: str, orderbook: OrderBook) -> None:
        buf = self._buffers.get(market_id)
        if buf:
            buf.update_orderbook(orderbook)

    def ingest_trade(self, market_id: str, trade: Trade) -> None:
        buf = self._buffers.get(market_id)
        if buf:
            buf.add_trade(trade)

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
