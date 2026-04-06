"""Latency arbitrage — sub-second exchange-to-Polymarket price gap trading.

Entry bounds 25¢-75¢, sigmoid k=800, with debug logging showing
why signals pass or fail each cycle so we can diagnose no-trade periods.
"""

from __future__ import annotations

import math
import time

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy

import structlog
logger = structlog.get_logger()


def btc_move_to_fair_probability(move_pct: float) -> float:
    k = 800.0
    prob = 1.0 / (1.0 + math.exp(-k * move_pct))
    return max(0.05, min(0.95, prob))


MAX_ENTRY_PRICE = 0.75
MIN_ENTRY_PRICE = 0.25


class LatencyArbStrategy(BaseStrategy):

    def __init__(
        self,
        min_gap_pct: float = 0.01,
        max_gap_pct: float = 0.25,
        min_exchange_move_pct: float = 0.002,
        confidence_floor: float = 0.55,
        size_pct: float = 0.04,
        fee_buffer_pct: float = 0.003,
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
        self._last_debug = 0.0

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
        poly_mid = snapshot.orderbook.mid_price

        # Entry price guard
        if poly_mid > MAX_ENTRY_PRICE or poly_mid < MIN_ENTRY_PRICE:
            return None

        # Detect move across sub-second timeframes
        move_500ms = self._exchange_feed.price_change_since(0.5)
        move_1s = self._exchange_feed.price_change_since(1.0)
        move_2s = self._exchange_feed.price_change_since(2.0)
        move_5s = self._exchange_feed.price_change_since(5.0)
        move_10s = self._exchange_feed.price_change_since(10.0)
        micro_mom = self._exchange_feed.micro_momentum()

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

        # Debug logging every 30 seconds to show what's happening
        now = time.time()
        if now - self._last_debug > 30:
            self._last_debug = now
            fair = btc_move_to_fair_probability(move_5s) if move_5s != 0 else 0.5
            raw_gap = abs(fair - poly_mid) if move_5s > 0 else abs(poly_mid - fair)
            logger.debug(
                "latarb_check",
                m=snapshot.market.id[:12],
                poly=round(poly_mid, 3),
                btc=round(exchange_price, 1),
                mv500ms=f"{move_500ms:+.4%}",
                mv5s=f"{move_5s:+.4%}",
                mv10s=f"{move_10s:+.4%}",
                fair=round(fair, 3),
                gap=round(raw_gap, 3),
                mom=round(micro_mom, 4),
                best=f"{best_move:+.4%}" if best_move else "none",
            )

        if best_move == 0.0:
            return None

        # Confirm direction with micro-momentum
        if best_move > 0 and micro_mom < -0.0002:
            return None
        if best_move < 0 and micro_mom > 0.0002:
            return None

        # Fair value via sigmoid (k=800)
        fair_yes = btc_move_to_fair_probability(best_move)

        if best_move > 0:
            gap = fair_yes - poly_mid
        else:
            gap = poly_mid - fair_yes

        effective_gap = abs(gap) - self._fee_buffer_pct

        if effective_gap < self._min_gap_pct:
            return None
        if abs(gap) > self._max_gap_pct:
            return None

        # Direction + entry price check
        if best_move > 0:
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

        # Confidence
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
