"""Latency arbitrage — sub-second exchange-to-Polymarket price gap trading.

Fair value model calibrated to real 5-minute BTC price action:
- Typical 5m move: $20-$80 (0.03%-0.12%)
- Large 5m move: $100-$200 (0.15%-0.30%)
- These small $ moves are DECISIVE for binary up/down resolution

Sigmoid steepness k=800 so that:
  $20 move (0.03%) → fair ≈ 0.56  (6¢ gap vs stale 50¢ book)
  $50 move (0.07%) → fair ≈ 0.64  (14¢ gap)
  $100 move (0.15%) → fair ≈ 0.77 (27¢ gap)
  $200 move (0.30%) → fair ≈ 0.91 (41¢ gap)
"""

from __future__ import annotations

import math

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


def btc_move_to_fair_probability(move_pct: float) -> float:
    """Convert BTC % move to fair Up probability.
    
    k=800 calibrated for real 5-minute BTC price action where
    $20-200 moves ($67k BTC = 0.03%-0.30%) are the norm.
    """
    k = 800.0
    prob = 1.0 / (1.0 + math.exp(-k * move_pct))
    return max(0.05, min(0.95, prob))


class LatencyArbStrategy(BaseStrategy):
    """Detects sub-second exchange-to-Polymarket price gaps."""

    def __init__(
        self,
        min_gap_pct: float = 0.015,
        max_gap_pct: float = 0.20,
        min_exchange_move_pct: float = 0.003,
        confidence_floor: float = 0.55,
        size_pct: float = 0.04,
        fee_buffer_pct: float = 0.005,
        market_keywords: list[str] | None = None,
    ) -> None:
        self._min_gap_pct = min_gap_pct
        self._max_gap_pct = max_gap_pct
        self._min_exchange_move_pct = min_exchange_move_pct
        self._confidence_floor = confidence_floor
        self._size_pct = size_pct
        self._fee_buffer_pct = fee_buffer_pct
        self._market_keywords = market_keywords
        self._exchange_feed: PriceFeedState | None = None

    @property
    def name(self) -> str:
        return "latency_arb"

    def set_exchange_feed(self, feed: PriceFeedState) -> None:
        self._exchange_feed = feed

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        if not self._exchange_feed or self._exchange_feed.last_price == 0:
            return None

        if len(self._exchange_feed.ticks) < 20:
            return None

        exchange_price = self._exchange_feed.last_price

        # ── Detect move across sub-second timeframes ──
        move_500ms = self._exchange_feed.price_change_since(0.5)
        move_1s = self._exchange_feed.price_change_since(1.0)
        move_2s = self._exchange_feed.price_change_since(2.0)
        move_5s = self._exchange_feed.price_change_since(5.0)
        move_10s = self._exchange_feed.price_change_since(10.0)

        micro_mom = self._exchange_feed.micro_momentum()

        # Take the fastest confirmed move (speed premium on thresholds)
        best_move = 0.0
        move_window = "none"

        checks = [
            (move_500ms, self._min_exchange_move_pct * 0.4, "500ms"),
            (move_1s,    self._min_exchange_move_pct * 0.6, "1s"),
            (move_2s,    self._min_exchange_move_pct * 0.8, "2s"),
            (move_5s,    self._min_exchange_move_pct,       "5s"),
            (move_10s,   self._min_exchange_move_pct * 1.2, "10s"),
        ]

        for move, threshold, window in checks:
            if abs(move) >= threshold:
                best_move = move
                move_window = window
                break

        if best_move == 0.0:
            return None

        # Confirm direction with micro-momentum
        if best_move > 0 and micro_mom < -0.0002:
            return None
        if best_move < 0 and micro_mom > 0.0002:
            return None

        # ── Fair value via sigmoid (k=800) ──
        fair_yes = btc_move_to_fair_probability(best_move)
        poly_mid = snapshot.orderbook.mid_price

        if best_move > 0:
            gap = fair_yes - poly_mid
        else:
            gap = poly_mid - fair_yes

        effective_gap = abs(gap) - self._fee_buffer_pct

        if effective_gap < self._min_gap_pct:
            return None

        if abs(gap) > self._max_gap_pct:
            return None

        # ── Direction ──
        if best_move > 0:
            direction = Direction.BUY
            outcome = "Yes"
            target_price = snapshot.orderbook.best_ask
        else:
            direction = Direction.SELL
            outcome = "No"
            target_price = snapshot.orderbook.best_bid

        # ── Confidence ──
        speed_bonus = {"500ms": 0.12, "1s": 0.08, "2s": 0.04, "5s": 0.0, "10s": 0.0}
        gap_score = min(effective_gap / 0.10, 1.0)
        move_score = min(abs(best_move) / 0.002, 1.0)

        confidence = (
            gap_score * 0.45
            + move_score * 0.35
            + speed_bonus.get(move_window, 0)
            + min(abs(micro_mom) * 500, 0.1)
        )
        confidence = max(min(confidence, 0.95), self._confidence_floor)

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=target_price,
            confidence=confidence,
            size_pct=self._size_pct * confidence,
            reason=(
                f"[{move_window}] BTC {best_move:+.4%} (${exchange_price * abs(best_move):.0f}) "
                f"fair={fair_yes:.3f} poly={poly_mid:.3f} gap={effective_gap:+.3f}"
            ),
            metadata={
                "exchange_price": exchange_price,
                "best_move": best_move,
                "move_window": move_window,
                "micro_momentum": micro_mom,
                "poly_mid": poly_mid,
                "fair_yes": fair_yes,
                "gap": gap,
                "effective_gap": effective_gap,
                "btc_dollar_move": exchange_price * abs(best_move),
                "move_500ms": move_500ms,
                "move_1s": move_1s,
                "move_2s": move_2s,
                "move_5s": move_5s,
                "move_10s": move_10s,
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
