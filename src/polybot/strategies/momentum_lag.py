"""Momentum lag — exploit delayed Polymarket reactions to BTC micro-moves.

When BTC trends consistently for 2-10 seconds, the 5-minute markets
lag behind. This catches the 1-5 second delay between Binance tick
and Polymarket orderbook adjustment.
"""

from __future__ import annotations

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


class MomentumLagStrategy(BaseStrategy):

    def __init__(
        self,
        min_move_30s_pct: float = 0.003,
        min_move_60s_pct: float = 0.006,
        min_gap_pct: float = 0.015,
        max_gap_pct: float = 0.15,
        thin_book_threshold: float = 0.015,
        size_pct: float = 0.04,
        market_keywords: list[str] | None = None,
    ) -> None:
        self._min_move_2s = min_move_30s_pct * 0.3   # Derived: ~0.001 for 2s
        self._min_move_5s = min_move_30s_pct * 0.5    # ~0.0015 for 5s
        self._min_move_10s = min_move_30s_pct * 0.8   # ~0.0024 for 10s
        self._min_move_30s = min_move_30s_pct
        self._min_move_60s = min_move_60s_pct
        self._min_gap_pct = min_gap_pct
        self._max_gap_pct = max_gap_pct
        self._thin_book_threshold = thin_book_threshold
        self._size_pct = size_pct
        self._market_keywords = market_keywords
        self._exchange_feed: PriceFeedState | None = None

    @property
    def name(self) -> str:
        return "momentum_lag"

    def set_exchange_feed(self, feed: PriceFeedState) -> None:
        self._exchange_feed = feed

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        if not self._exchange_feed or len(self._exchange_feed.ticks) < 20:
            return None

        # ── Check for consistent directional move ──
        move_2s = self._exchange_feed.price_change_since(2.0)
        move_5s = self._exchange_feed.price_change_since(5.0)
        move_10s = self._exchange_feed.price_change_since(10.0)
        move_30s = self._exchange_feed.price_change_since(30.0)

        # Need at least one timeframe to show a real move
        has_fast = abs(move_2s) >= self._min_move_2s or abs(move_5s) >= self._min_move_5s
        has_medium = abs(move_10s) >= self._min_move_10s or abs(move_30s) >= self._min_move_30s

        if not (has_fast or has_medium):
            return None

        # All non-zero moves must agree on direction
        moves = [m for m in [move_2s, move_5s, move_10s, move_30s] if abs(m) > 0.00005]
        if not moves:
            return None
        if not all(m > 0 for m in moves) and not all(m < 0 for m in moves):
            return None  # Mixed signals, skip

        # Use the fastest confirmed move as the signal
        move_pct = moves[0]  # Already sorted fast→slow by the list order

        # ── Check if Polymarket book is lagging ──
        poly_mid = snapshot.orderbook.mid_price
        spread = snapshot.orderbook.spread
        is_thin = spread >= self._thin_book_threshold

        # Fair value: same mapping as latency_arb
        move_abs = abs(move_pct)
        shift = min(move_abs * 150, 0.42)

        if move_pct > 0:
            fair_yes = 0.50 + shift
            gap = fair_yes - poly_mid
        else:
            fair_yes = 0.50 - shift
            gap = poly_mid - fair_yes

        if gap < self._min_gap_pct or gap > self._max_gap_pct:
            return None

        # ── Direction ──
        if move_pct > 0:
            direction = Direction.BUY
            outcome = "Yes"
        else:
            direction = Direction.SELL
            outcome = "No"

        # ── Confidence ──
        move_score = min(move_abs / (self._min_move_30s * 2), 1.0)
        gap_score = min(gap / (self._min_gap_pct * 2.5), 1.0)
        thin_bonus = 0.1 if is_thin else 0.0
        fast_bonus = 0.1 if has_fast else 0.0
        confidence = min(move_score * 0.35 + gap_score * 0.35 + thin_bonus + fast_bonus, 0.95)

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=poly_mid,
            confidence=confidence,
            size_pct=self._size_pct * confidence,
            reason=(
                f"MomLag: 2s={move_2s:+.4%} 5s={move_5s:+.4%} 10s={move_10s:+.4%} "
                f"gap={gap:.3f} spread={spread:.3f}"
            ),
            metadata={
                "move_2s": move_2s,
                "move_5s": move_5s,
                "move_10s": move_10s,
                "move_30s": move_30s,
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
