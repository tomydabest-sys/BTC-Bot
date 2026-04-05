"""Momentum lag — trades sustained BTC trends vs stale Polymarket books.

Same entry price guards as latency_arb: only enter between 35¢-65¢.
"""

from __future__ import annotations

import math

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


def btc_move_to_fair_probability(move_pct: float) -> float:
    k = 800.0
    prob = 1.0 / (1.0 + math.exp(-k * move_pct))
    return max(0.05, min(0.95, prob))


MAX_ENTRY_PRICE = 0.65
MIN_ENTRY_PRICE = 0.35


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
        if not self._exchange_feed or len(self._exchange_feed.ticks) < 30:
            return None

        poly_mid = snapshot.orderbook.mid_price

        # Entry price guard
        if poly_mid > MAX_ENTRY_PRICE or poly_mid < MIN_ENTRY_PRICE:
            return None

        move_30s = self._exchange_feed.price_change_pct(30)
        move_60s = self._exchange_feed.price_change_pct(60)

        strong_30s = abs(move_30s) >= self._min_move_30s
        strong_60s = abs(move_60s) >= self._min_move_60s

        if not (strong_30s or strong_60s):
            return None
        if move_30s * move_60s < 0:
            return None

        move_pct = move_30s if strong_30s else move_60s

        spread = snapshot.orderbook.spread
        is_thin = spread >= self._thin_book_threshold

        if not is_thin and abs(move_60s) < self._min_move_60s * 1.5:
            return None

        fair_yes = btc_move_to_fair_probability(move_pct)

        if move_pct > 0:
            gap = fair_yes - poly_mid
        else:
            gap = poly_mid - fair_yes

        if gap < self._min_gap_pct or gap > self._max_gap_pct:
            return None

        if move_pct > 0:
            direction = Direction.BUY
            outcome = "Yes"
            target_price = snapshot.orderbook.best_ask
            if target_price > MAX_ENTRY_PRICE:
                return None
        else:
            direction = Direction.SELL
            outcome = "No"
            target_price = snapshot.orderbook.best_bid
            if target_price < MIN_ENTRY_PRICE:
                return None

        move_score = min(abs(move_pct) / (self._min_move_60s * 3), 1.0)
        gap_score = min(gap / 0.08, 1.0)
        thin_bonus = 0.08 if is_thin else 0.0
        confidence = min(move_score * 0.45 + gap_score * 0.4 + thin_bonus, 0.95)

        exchange_price = self._exchange_feed.last_price

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=target_price,
            confidence=confidence,
            size_pct=self._size_pct * confidence,
            reason=(
                f"Momentum {move_30s:+.3%}(30s) {move_60s:+.3%}(60s) "
                f"(${exchange_price * abs(move_pct):.0f}) "
                f"fair={fair_yes:.3f} poly={poly_mid:.3f} gap={gap:.3f}"
            ),
            metadata={
                "move_30s": move_30s,
                "move_60s": move_60s,
                "fair_yes": fair_yes,
                "gap": gap,
                "spread": spread,
                "is_thin_book": is_thin,
                "exchange_price": exchange_price,
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
