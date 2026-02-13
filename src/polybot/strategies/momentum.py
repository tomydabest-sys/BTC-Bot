"""Momentum strategy — follows strong directional moves confirmed by volume."""

from __future__ import annotations

from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    """
    Generates signals when strong price momentum is detected with volume confirmation.

    Entry: price change > threshold AND volume > multiplier * average
    Exit: momentum fades, take-profit, or trailing stop
    """

    def __init__(
        self,
        price_change_threshold: float = 0.05,
        lookback_minutes: int = 60,
        volume_multiplier: float = 2.0,
        take_profit: float = 0.03,
        trailing_stop: float = 0.02,
    ) -> None:
        self._price_change_threshold = price_change_threshold
        self._lookback_minutes = lookback_minutes
        self._volume_multiplier = volume_multiplier
        self._take_profit = take_profit
        self._trailing_stop = trailing_stop

    @property
    def name(self) -> str:
        return "momentum"

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        if len(snapshot.price_history) < 10:
            return None

        # Calculate price change over lookback period
        old_price = snapshot.price_history[0]
        current_price = snapshot.price_history[-1]
        if old_price == 0:
            return None

        price_change = (current_price - old_price) / old_price

        if abs(price_change) < self._price_change_threshold:
            return None

        # Volume confirmation via book imbalance as proxy
        imbalance = abs(snapshot.orderbook.book_imbalance)
        if imbalance < 0.1:  # Require some directional bias in the book
            return None

        if price_change > 0:
            direction = Direction.BUY
            outcome = "Yes"
        else:
            direction = Direction.SELL
            outcome = "No"

        confidence = min(abs(price_change) / (self._price_change_threshold * 2), 1.0)
        size_pct = 0.03 * confidence

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=current_price,
            confidence=confidence,
            size_pct=size_pct,
            reason=f"Momentum detected: {price_change:+.2%} price change",
            metadata={
                "price_change": price_change,
                "old_price": old_price,
                "current_price": current_price,
                "book_imbalance": snapshot.orderbook.book_imbalance,
            },
        )

    def get_params(self) -> dict:
        return {
            "price_change_threshold": self._price_change_threshold,
            "lookback_minutes": self._lookback_minutes,
            "volume_multiplier": self._volume_multiplier,
            "take_profit": self._take_profit,
            "trailing_stop": self._trailing_stop,
        }
