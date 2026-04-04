"""Volatility breakout strategy — detects compression-to-expansion patterns.

Based on the Jane Street bot analysis: when volatility compresses on
ultra-short timeframes and then breaks directionally, the 5-15 minute
crypto markets lag by 30-90 seconds. This strategy scans for volatility
compression into directional breaks.
"""

from __future__ import annotations

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy
from polybot.strategies.market_filter import is_crypto_window_market


class VolatilityBreakoutStrategy(BaseStrategy):
    """Trades volatility compression breakouts on crypto prediction markets."""

    def __init__(
        self,
        compression_ratio: float = 0.4,
        breakout_threshold_pct: float = 0.02,
        lookback_compress_seconds: int = 120,
        lookback_baseline_seconds: int = 600,
        min_spread: float = 0.03,
        size_pct: float = 0.06,
        market_keywords: list[str] | None = None,
    ) -> None:
        self._compression_ratio = compression_ratio
        self._breakout_threshold_pct = breakout_threshold_pct
        self._lookback_compress = lookback_compress_seconds
        self._lookback_baseline = lookback_baseline_seconds
        self._min_spread = min_spread
        self._size_pct = size_pct
        self._market_keywords = market_keywords
        self._exchange_feed: PriceFeedState | None = None

    @property
    def name(self) -> str:
        return "volatility_breakout"

    def set_exchange_feed(self, feed: PriceFeedState) -> None:
        self._exchange_feed = feed

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        if not self._exchange_feed or len(self._exchange_feed.ticks) < 20:
            return None

       # Scanner guarantees only BTC up/down markets reach here.
       # No keyword filtering needed.

        # Step 1: Measure volatility compression
        recent_vol = self._exchange_feed.volatility_window(self._lookback_compress)
        baseline_vol = self._exchange_feed.volatility_window(self._lookback_baseline)

        if baseline_vol == 0:
            return None

        vol_ratio = recent_vol / baseline_vol

        # Not compressed enough
        if vol_ratio > self._compression_ratio:
            return None

        # Step 2: Check for breakout — price moving sharply after compression
        move_5s = self._exchange_feed.price_change_pct(5)
        move_15s = self._exchange_feed.price_change_pct(15)

        # Need a meaningful move after compression
        if abs(move_5s) < self._breakout_threshold_pct:
            return None

        # Confirm direction consistency (5s and 15s same sign)
        if move_5s * move_15s < 0:
            return None  # Mixed signals

        # Step 3: Check Polymarket hasn't caught up yet
        poly_mid = snapshot.orderbook.mid_price
        spread = snapshot.orderbook.spread

        if spread < self._min_spread:
            return None  # Book is tight, likely already priced in

        # Step 4: Generate signal
        if move_5s > 0:
            direction = Direction.BUY
            outcome = "Yes"
        else:
            direction = Direction.SELL
            outcome = "No"

        # Confidence: stronger breakout from deeper compression = higher confidence
        breakout_strength = min(abs(move_5s) / (self._breakout_threshold_pct * 3), 1.0)
        compression_depth = min((1.0 - vol_ratio) / 0.6, 1.0)
        confidence = breakout_strength * 0.6 + compression_depth * 0.4
        confidence = min(confidence, 0.95)

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=poly_mid,
            confidence=confidence,
            size_pct=self._size_pct * confidence,
            reason=(
                f"Vol compression ({vol_ratio:.2f}x baseline) → "
                f"breakout {move_5s:+.2%} in 5s"
            ),
            metadata={
                "vol_ratio": vol_ratio,
                "recent_vol": recent_vol,
                "baseline_vol": baseline_vol,
                "move_5s": move_5s,
                "move_15s": move_15s,
                "spread": spread,
            },
        )

    def get_params(self) -> dict:
        return {
            "compression_ratio": self._compression_ratio,
            "breakout_threshold_pct": self._breakout_threshold_pct,
            "lookback_compress_seconds": self._lookback_compress,
            "lookback_baseline_seconds": self._lookback_baseline,
            "min_spread": self._min_spread,
            "size_pct": self._size_pct,
        }
