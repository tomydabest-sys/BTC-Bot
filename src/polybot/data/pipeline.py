"""Data ingestion, normalization, and derived metrics.

Adds a Polymarket mid-price ring buffer per market so strategies can observe
short-window Polymarket movement (poly_move_5s, poly_move_30s) without needing
to keep their own state.
"""

from __future__ import annotations

import statistics
import time
from collections import deque
from datetime import datetime, timedelta

from polybot.data.models import Market, MarketSnapshot, OrderBook, Trade


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
        # Dedup: avoid multiple samples within a single tick cycle
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
            # Data doesn't reach back that far — use oldest sample
            if (now - self.mid_history[0][0]) < seconds * 0.5:
                return 0.0
            old_mid = self.mid_history[0][1]
        return latest_mid - old_mid


class DataPipeline:
    """Ingests and serves normalized market data."""

    def __init__(self) -> None:
        self._buffers: dict[str, MarketDataBuffer] = {}
        self._markets: dict[str, Market] = {}

    def register_market(self, market: Market) -> None:
        self._markets[market.id] = market
        if market.id not in self._buffers:
            self._buffers[market.id] = MarketDataBuffer()

    def unregister_market(self, market_id: str) -> None:
        self._markets.pop(market_id, None)
        self._buffers.pop(market_id, None)

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

        poly_move_5s = buf.poly_move_over(5.0)
        poly_move_30s = buf.poly_move_over(30.0)

        return MarketSnapshot(
            market=market,
            orderbook=buf.orderbook,
            recent_trades=trades_1h,
            vwap_1h=self._compute_vwap(trades_1h),
            vwap_24h=self._compute_vwap(trades_24h),
            volatility_1h=self._compute_volatility(trades_1h),
            price_history=list(buf.price_history),
            poly_move_5s=poly_move_5s,
            poly_move_30s=poly_move_30s,
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
