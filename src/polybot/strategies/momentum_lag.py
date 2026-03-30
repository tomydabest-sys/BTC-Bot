"""Momentum lag strategy — exploit delayed Polymarket reactions to crypto moves.

From the Jane Street bot analysis: when crypto moves hard in one direction,
short-window Polymarket markets lag by 30-90 seconds. The order books thin
out, prices stick, and the market freezes while the asset keeps moving.

This strategy watches for 3-5% gaps between where the market should be
priced and where it actually is, specifically on ultra-short timeframes.
"""

from __future__ import annotations

import time

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


class MomentumLagStrategy(BaseStrategy):
    """Trades the lag between exchange momentum and Polymarket pricing."""

    def __init__(
        self,
        min_move_30s_pct: float = 0.015,
        min_move_60s_pct: float = 0.025,
        min_gap_pct: float = 0.03,
        max_gap_pct: float = 0.10,
        thin_book_threshold: float = 0.04,
        size_pct: float = 0.07,
    ) -> None:
        self._min_move_30s = min_move_30s_pct
        self._min_move_60s = min_move_60s_pct
        self._min_gap_pct = min_gap_pct
        self._max_gap_pct = max_gap_pct
        self._thin_book_threshold = thin_book_threshold
        self._size_pct = size_pct
        self._exchange_feed: PriceFeedState | None = None

    @property
    def name(self) -> str:
        return "momentum_lag"

    def set_exchange_feed(self, feed: PriceFeedState) -> None:
        self._exchange_feed = feed

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        if not self._exchange_feed or len(self._exchange_feed.ticks) < 30:
            return None

        # Target crypto prediction markets
        question = snapshot.market.question.lower()
        is_crypto_window = any(
            kw in question
            for kw in ["up or down", "higher or lower", "5 min", "15 min", "minute"]
        )
        if not is_crypto_window:
            return None

        # Check for strong directional exchange move
        move_30s = self._exchange_feed.price_change_pct(30)
        move_60s = self._exchange_feed.price_change_pct(60)

        # Need consistent strong move
        strong_30s = abs(move_30s) >= self._min_move_30s
        strong_60s = abs(move_60s) >= self._min_move_60s

        if not (strong_30s or strong_60s):
            return None

        # Both should agree on direction
        if move_30s * move_60s < 0:
            return None

        # Check if Polymarket book is thin (sign of lagging)
        spread = snapshot.orderbook.spread
        is_thin = spread >= self._thin_book_threshold

        # Even without thin book, proceed if exchange move is very strong
        if not is_thin and abs(move_60s) < self._min_move_60s * 2:
            return None

        # Estimate where Polymarket should be priced
        poly_mid = snapshot.orderbook.mid_price
        move_pct = move_30s if strong_30s else move_60s

        # For "will price go up" markets
        if move_pct > 0:
            # Price going up → Yes should be high
            fair_yes = min(0.5 + abs(move_pct) * 8, 0.92)
            gap = fair_yes - poly_mid
        else:
            # Price going down → Yes should be low
            fair_yes = max(0.5 - abs(move_pct) * 8, 0.08)
            gap = poly_mid - fair_yes

        if gap < self._min_gap_pct or gap > self._max_gap_pct:
            return None

        # Direction
        if move_pct > 0:
            direction = Direction.BUY
            outcome = "Yes"
        else:
            direction = Direction.SELL
            outcome = "No"

        # Confidence from move strength, book thinness, and gap size
        move_score = min(abs(move_pct) / (self._min_move_60s * 3), 1.0)
        gap_score = min(gap / (self._min_gap_pct * 3), 1.0)
        thin_bonus = 0.1 if is_thin else 0.0
        confidence = min(move_score * 0.5 + gap_score * 0.4 + thin_bonus, 0.95)

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=poly_mid,
            confidence=confidence,
            size_pct=self._size_pct * confidence,
            reason=(
                f"Momentum lag: exchange {move_30s:+.2%} (30s) / {move_60s:+.2%} (60s), "
                f"Polymarket gap={gap:.4f}, spread={spread:.4f}"
            ),
            metadata={
                "move_30s": move_30s,
                "move_60s": move_60s,
                "fair_yes": fair_yes,
                "gap": gap,
                "spread": spread,
                "is_thin_book": is_thin,
                "exchange_price": self._exchange_feed.last_price,
            },
        )

    def get_params(self) -> dict:
        return {
            "min_move_30s_pct": self._min_move_30s,
            "min_move_60s_pct": self._min_move_60s,
            "min_gap_pct": self._min_gap_pct,
            "max_gap_pct": self._max_gap_pct,
            "thin_book_threshold": self._thin_book_threshold,
            "size_pct": self._size_pct,
        }
