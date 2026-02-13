"""Mean reversion strategy — trades toward fair value when price deviates."""

from __future__ import annotations

from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    """
    Generates signals when the mid price deviates from VWAP-based fair value.

    Entry: |mid - fair_value| > deviation_threshold
    Exit: price reverts within exit_threshold of fair value
    Stop-loss: 2x entry deviation
    """

    def __init__(
        self,
        deviation_threshold: float = 0.03,
        exit_threshold: float = 0.01,
        stop_loss_multiplier: float = 2.0,
        lookback_minutes: int = 60,
        max_hold_minutes: int = 120,
    ) -> None:
        self._deviation_threshold = deviation_threshold
        self._exit_threshold = exit_threshold
        self._stop_loss_multiplier = stop_loss_multiplier
        self._lookback_minutes = lookback_minutes
        self._max_hold_minutes = max_hold_minutes

    @property
    def name(self) -> str:
        return "mean_reversion"

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        fair_value = self._estimate_fair_value(snapshot)
        if fair_value == 0.0:
            return None

        mid = snapshot.orderbook.mid_price
        deviation = mid - fair_value

        if abs(deviation) < self._deviation_threshold:
            return None

        # Price is above fair value → sell (expect reversion down)
        # Price is below fair value → buy (expect reversion up)
        if deviation > 0:
            direction = Direction.SELL
            outcome = "No"
            target_price = mid  # Sell at current elevated price
        else:
            direction = Direction.BUY
            outcome = "Yes"
            target_price = mid  # Buy at current depressed price

        confidence = min(abs(deviation) / (self._deviation_threshold * 3), 1.0)
        size_pct = 0.05 * confidence  # 1–5% of capital based on confidence

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=target_price,
            confidence=confidence,
            size_pct=size_pct,
            reason=f"Price deviated {deviation:+.4f} from fair value {fair_value:.4f}",
            metadata={
                "fair_value": fair_value,
                "deviation": deviation,
                "mid_price": mid,
            },
        )

    def get_params(self) -> dict:
        return {
            "deviation_threshold": self._deviation_threshold,
            "exit_threshold": self._exit_threshold,
            "stop_loss_multiplier": self._stop_loss_multiplier,
            "lookback_minutes": self._lookback_minutes,
            "max_hold_minutes": self._max_hold_minutes,
        }

    def _estimate_fair_value(self, snapshot: MarketSnapshot) -> float:
        """Estimate fair value using VWAP and book imbalance."""
        if snapshot.vwap_1h == 0.0:
            return snapshot.vwap_24h

        base = snapshot.vwap_1h
        # Adjust for order book imbalance
        imbalance = snapshot.orderbook.book_imbalance
        adjustment = imbalance * 0.01  # Small shift based on order flow
        return base + adjustment
