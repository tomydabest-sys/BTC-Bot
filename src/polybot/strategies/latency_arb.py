"""Latency arbitrage — exploits price lag between exchanges and Polymarket.

Inspired by the OpenClaw/Fast-Loop approach: when a crypto asset moves on
Binance/Coinbase, Polymarket's 5-15 minute up/down markets lag by 20-90ms
(or more on thin books). This strategy detects the gap and trades before
Polymarket catches up.

Post-fee-change adaptation: instead of pure speed, this now focuses on
larger gaps (3-5%) where the edge exceeds dynamic fees.
"""

from __future__ import annotations

import time

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


class LatencyArbStrategy(BaseStrategy):
    """Detects exchange-to-Polymarket price gaps and trades the lag."""

    def __init__(
        self,
        min_gap_pct: float = 0.03,
        max_gap_pct: float = 0.15,
        min_exchange_move_pct: float = 0.02,
        confidence_floor: float = 0.6,
        size_pct: float = 0.08,
        fee_buffer_pct: float = 0.01,
    ) -> None:
        self._min_gap_pct = min_gap_pct
        self._max_gap_pct = max_gap_pct
        self._min_exchange_move_pct = min_exchange_move_pct
        self._confidence_floor = confidence_floor
        self._size_pct = size_pct
        self._fee_buffer_pct = fee_buffer_pct
        self._exchange_feed: PriceFeedState | None = None

    @property
    def name(self) -> str:
        return "latency_arb"

    def set_exchange_feed(self, feed: PriceFeedState) -> None:
        self._exchange_feed = feed

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        if not self._exchange_feed or self._exchange_feed.last_price == 0:
            return None

        # Only works on crypto up/down markets
        question = snapshot.market.question.lower()
        is_up_down = any(
            kw in question
            for kw in ["up or down", "higher or lower", "above or below", "increase or decrease"]
        )
        if not is_up_down:
            return None

        # Determine which direction the exchange is moving
        exchange_price = self._exchange_feed.last_price
        exchange_5s = self._exchange_feed.price_5s_ago
        if exchange_5s == 0:
            return None

        exchange_move_pct = (exchange_price - exchange_5s) / exchange_5s

        # Need a meaningful exchange move
        if abs(exchange_move_pct) < self._min_exchange_move_pct:
            return None

        # Polymarket mid should reflect this move but might lag
        poly_mid = snapshot.orderbook.mid_price

        # For "up" markets: if exchange is pumping, Yes should be > 0.5
        # For "down" markets: if exchange is dumping, Yes should be > 0.5
        # The gap is the discrepancy

        if exchange_move_pct > 0:
            # Exchange going up → "Yes" on up market should be high
            # If poly_mid is still low, there's a gap to exploit
            fair_yes = min(0.5 + abs(exchange_move_pct) * 5, 0.95)
            gap = fair_yes - poly_mid
        else:
            # Exchange going down → "No" on up market should be high
            fair_yes = max(0.5 - abs(exchange_move_pct) * 5, 0.05)
            gap = poly_mid - fair_yes  # positive gap means poly is too high

        # Adjust for fees
        effective_gap = abs(gap) - self._fee_buffer_pct

        if effective_gap < self._min_gap_pct:
            return None

        if abs(gap) > self._max_gap_pct:
            return None  # Too wide, might be stale data

        # Determine direction
        if gap > 0 and exchange_move_pct > 0:
            direction = Direction.BUY
            outcome = "Yes"
            target_price = poly_mid
        elif gap > 0 and exchange_move_pct < 0:
            direction = Direction.SELL
            outcome = "No"
            target_price = poly_mid
        else:
            return None

        # Confidence scales with gap size and exchange move strength
        gap_confidence = min(effective_gap / (self._min_gap_pct * 3), 1.0)
        move_confidence = min(abs(exchange_move_pct) / (self._min_exchange_move_pct * 3), 1.0)
        confidence = max((gap_confidence * 0.6 + move_confidence * 0.4), self._confidence_floor)
        confidence = min(confidence, 1.0)

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=target_price,
            confidence=confidence,
            size_pct=self._size_pct * confidence,
            reason=(
                f"Exchange moved {exchange_move_pct:+.2%} in 5s, "
                f"Polymarket gap {gap:+.4f} (net {effective_gap:+.4f} after fees)"
            ),
            metadata={
                "exchange_price": exchange_price,
                "exchange_move_pct": exchange_move_pct,
                "poly_mid": poly_mid,
                "fair_yes": fair_yes,
                "gap": gap,
                "effective_gap": effective_gap,
            },
        )

    def get_params(self) -> dict:
        return {
            "min_gap_pct": self._min_gap_pct,
            "max_gap_pct": self._max_gap_pct,
            "min_exchange_move_pct": self._min_exchange_move_pct,
            "confidence_floor": self._confidence_floor,
            "size_pct": self._size_pct,
            "fee_buffer_pct": self._fee_buffer_pct,
        }
