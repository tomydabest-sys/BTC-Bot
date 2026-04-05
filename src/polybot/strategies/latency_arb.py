"""Latency arbitrage — sub-second exchange-to-Polymarket price gap trading.

Tuned for 5-minute BTC Up/Down markets where:
- Markets open at ~50c and resolve in 5 minutes
- A $50 BTC move (0.07%) shifts fair value to 52-55c
- Polymarket books lag Binance by 1-5 seconds
- Edge = buying at 50c when Binance already says 53c
"""

from __future__ import annotations

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


class LatencyArbStrategy(BaseStrategy):

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
        if len(self._exchange_feed.ticks) < 10:
            return None

        exchange_price = self._exchange_feed.last_price

        # ── Detect exchange move (fastest window first) ──
        move_500ms = self._exchange_feed.price_change_since(0.5)
        move_1s = self._exchange_feed.price_change_since(1.0)
        move_2s = self._exchange_feed.price_change_since(2.0)
        move_5s = self._exchange_feed.price_change_since(5.0)
        move_10s = self._exchange_feed.price_change_since(10.0)
        micro_mom = self._exchange_feed.micro_momentum()

        threshold = self._min_exchange_move_pct
        best_move = 0.0
        move_window = "none"

        if abs(move_500ms) >= threshold * 0.5:
            best_move = move_500ms
            move_window = "500ms"
        elif abs(move_1s) >= threshold * 0.6:
            best_move = move_1s
            move_window = "1s"
        elif abs(move_2s) >= threshold * 0.8:
            best_move = move_2s
            move_window = "2s"
        elif abs(move_5s) >= threshold:
            best_move = move_5s
            move_window = "5s"
        elif abs(move_10s) >= threshold * 1.2:
            best_move = move_10s
            move_window = "10s"

        if best_move == 0.0:
            return None

        # Confirm micro-momentum agrees with move direction
        if best_move > 0 and micro_mom < -0.0002:
            return None
        if best_move < 0 and micro_mom > 0.0002:
            return None

        # ── Fair value calculation ──
        # Each 0.01% BTC move ~ 1.5c on a 5-min prediction market
        # 0.05% move → fair=0.575, 0.10% → 0.65, 0.30% → 0.92(cap)
        poly_mid = snapshot.orderbook.mid_price
        move_abs = abs(best_move)
        shift = min(move_abs * 150, 0.42)

        if best_move > 0:
            fair_yes = 0.50 + shift
            gap = fair_yes - poly_mid
        else:
            fair_yes = 0.50 - shift
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
        speed_bonus = {"500ms": 0.15, "1s": 0.10, "2s": 0.05, "5s": 0.0, "10s": 0.0}
        gap_conf = min(effective_gap / (self._min_gap_pct * 2.5), 1.0)
        move_conf = min(move_abs / (self._min_exchange_move_pct * 2.5), 1.0)
        mom_conf = min(abs(micro_mom) * 500, 0.15)

        confidence = gap_conf * 0.35 + move_conf * 0.35 + speed_bonus.get(move_window, 0) + mom_conf
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
                f"[{move_window}] BTC {best_move:+.4%} "
                f"poly={poly_mid:.3f} fair={fair_yes:.3f} "
                f"gap={effective_gap:+.3f} mom={micro_mom:+.5f}"
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
