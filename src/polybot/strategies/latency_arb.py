"""Latency arbitrage — sub-second exchange-to-Polymarket price gap trading.

Core mechanic for 5-minute BTC Up/Down markets:
1. Binance WS delivers price ticks in ~50ms
2. Polymarket orderbook lags by 1-5 seconds (retail participants)
3. When BTC ticks up $30-100 on Binance, "Up" should be >50¢
4. But the Polymarket book is still sitting at 50¢
5. Buy "Up" at 50¢, wait for book to catch up or hold to resolution

This version uses sub-second price data (200ms, 500ms, 1s, 2s windows)
instead of the old 5-second minimum. The WebSocket feed provides the
raw speed; this strategy converts that into signals.
"""

from __future__ import annotations

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


class LatencyArbStrategy(BaseStrategy):
    """Detects sub-second exchange-to-Polymarket price gaps."""

    def __init__(
        self,
        min_gap_pct: float = 0.015,
        max_gap_pct: float = 0.15,
        min_exchange_move_pct: float = 0.005,
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

        # ── Detect exchange move across multiple timeframes ──
        # Check from fastest to slowest. Any confirmed move is tradeable.
        exchange_price = self._exchange_feed.last_price

        # Sub-second moves (fastest signal, from WebSocket)
        move_500ms = self._exchange_feed.price_change_since(0.5)
        move_1s = self._exchange_feed.price_change_since(1.0)
        move_2s = self._exchange_feed.price_change_since(2.0)
        move_5s = self._exchange_feed.price_change_since(5.0)

        # Micro-momentum: composite sub-second trend
        micro_mom = self._exchange_feed.micro_momentum()

        # Find the strongest confirmed move
        # Prioritize speed: a 0.1% move in 500ms is better than 0.2% in 5s
        best_move = 0.0
        move_window = "none"

        if abs(move_500ms) >= self._min_exchange_move_pct * 0.6:
            # 500ms move with lower threshold (speed premium)
            best_move = move_500ms
            move_window = "500ms"
        elif abs(move_1s) >= self._min_exchange_move_pct * 0.8:
            best_move = move_1s
            move_window = "1s"
        elif abs(move_2s) >= self._min_exchange_move_pct:
            best_move = move_2s
            move_window = "2s"
        elif abs(move_5s) >= self._min_exchange_move_pct:
            best_move = move_5s
            move_window = "5s"

        if best_move == 0.0:
            return None

        # ── Confirm direction with micro-momentum ──
        # If micro_momentum disagrees with the move, skip (reversal risk)
        if best_move > 0 and micro_mom < -0.0001:
            return None
        if best_move < 0 and micro_mom > 0.0001:
            return None

        # ── Calculate gap vs Polymarket ──
        poly_mid = snapshot.orderbook.mid_price

        if best_move > 0:
            # Exchange going up → "Up" should be > 50¢
            # Scale: a 0.1% BTC move ≈ 5¢ on a 5m market
            fair_yes = min(0.5 + abs(best_move) * 50, 0.92)
            gap = fair_yes - poly_mid
        else:
            # Exchange going down → "Up" should be < 50¢
            fair_yes = max(0.5 - abs(best_move) * 50, 0.08)
            gap = poly_mid - fair_yes  # positive = poly is too high

        # Adjust for fees
        effective_gap = abs(gap) - self._fee_buffer_pct

        if effective_gap < self._min_gap_pct:
            return None

        if abs(gap) > self._max_gap_pct:
            return None  # Too wide — stale data or crossed book

        # ── Direction ──
        if gap > 0 and best_move > 0:
            direction = Direction.BUY
            outcome = "Yes"
            target_price = poly_mid  # Buy at current mid
        elif gap > 0 and best_move < 0:
            direction = Direction.SELL
            outcome = "No"
            target_price = poly_mid
        else:
            return None

        # ── Confidence ──
        # Speed bonus: faster detection = higher confidence
        speed_bonus = {"500ms": 0.15, "1s": 0.10, "2s": 0.05, "5s": 0.0}
        gap_confidence = min(effective_gap / (self._min_gap_pct * 3), 1.0)
        move_confidence = min(abs(best_move) / (self._min_exchange_move_pct * 3), 1.0)
        confidence = (
            gap_confidence * 0.4
            + move_confidence * 0.4
            + speed_bonus.get(move_window, 0) 
            + abs(micro_mom) * 100  # micro-momentum boost
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
                f"Latency arb [{move_window}]: BTC {best_move:+.3%}, "
                f"poly={poly_mid:.3f}, fair={fair_yes:.3f}, "
                f"gap={effective_gap:+.3f}, μ-mom={micro_mom:+.5f}"
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
                "move_500ms": move_500ms,
                "move_1s": move_1s,
                "move_2s": move_2s,
                "move_5s": move_5s,
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
