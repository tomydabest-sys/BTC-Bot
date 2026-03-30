"""Market making strategy — provide liquidity and earn the spread.

Post fee-change, the winning bots are liquidity providers, not takers.
This strategy places limit orders on both sides of the book, capturing
the bid-ask spread while managing inventory risk.
"""

from __future__ import annotations

from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


class MarketMakerStrategy(BaseStrategy):
    """Provides two-sided liquidity and earns the spread."""

    def __init__(
        self,
        min_spread: float = 0.03,
        target_spread: float = 0.04,
        max_inventory_pct: float = 0.15,
        skew_factor: float = 0.5,
        size_pct: float = 0.04,
        max_position_age_minutes: int = 30,
    ) -> None:
        self._min_spread = min_spread
        self._target_spread = target_spread
        self._max_inventory_pct = max_inventory_pct
        self._skew_factor = skew_factor
        self._size_pct = size_pct
        self._max_position_age = max_position_age_minutes
        self._inventory: dict[str, float] = {}  # market_id → net inventory

    @property
    def name(self) -> str:
        return "market_maker"

    def update_inventory(self, market_id: str, delta: float) -> None:
        self._inventory[market_id] = self._inventory.get(market_id, 0) + delta

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        spread = snapshot.orderbook.spread
        mid = snapshot.orderbook.mid_price

        if spread < self._min_spread:
            return None  # Spread too tight, not profitable after fees

        if mid <= 0.05 or mid >= 0.95:
            return None  # Avoid extreme prices near resolution

        # Calculate inventory skew
        net_inventory = self._inventory.get(snapshot.market.id, 0.0)
        skew = -net_inventory * self._skew_factor

        # Decide which side to quote
        # If we have positive inventory (long), skew toward selling
        # If we have negative inventory (short), skew toward buying
        half_spread = self._target_spread / 2

        bid_price = mid - half_spread + skew
        ask_price = mid + half_spread + skew

        # Clamp to valid range
        bid_price = max(0.01, min(bid_price, 0.99))
        ask_price = max(0.01, min(ask_price, 0.99))

        # If inventory is too large, only reduce
        if abs(net_inventory) > self._max_inventory_pct:
            if net_inventory > 0:
                direction = Direction.SELL
                outcome = "No"
                target_price = ask_price
            else:
                direction = Direction.BUY
                outcome = "Yes"
                target_price = bid_price
        else:
            # Quote the more profitable side based on book imbalance
            imbalance = snapshot.orderbook.book_imbalance
            if imbalance > 0.1:
                # More buyers → sell to them
                direction = Direction.SELL
                outcome = "No"
                target_price = ask_price
            elif imbalance < -0.1:
                # More sellers → buy from them
                direction = Direction.BUY
                outcome = "Yes"
                target_price = bid_price
            else:
                # Balanced — buy side (default lean)
                direction = Direction.BUY
                outcome = "Yes"
                target_price = bid_price

        # Confidence based on spread opportunity
        spread_opportunity = spread - self._min_spread
        confidence = min(spread_opportunity / self._target_spread + 0.5, 0.9)
        confidence = max(confidence, 0.5)

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=target_price,
            confidence=confidence,
            size_pct=self._size_pct,
            reason=(
                f"MM: spread={spread:.4f}, mid={mid:.4f}, "
                f"inventory={net_inventory:+.2f}, skew={skew:+.4f}"
            ),
            metadata={
                "bid_price": bid_price,
                "ask_price": ask_price,
                "spread": spread,
                "net_inventory": net_inventory,
                "skew": skew,
                "book_imbalance": snapshot.orderbook.book_imbalance,
                "is_market_maker": True,
            },
        )

    def get_params(self) -> dict:
        return {
            "min_spread": self._min_spread,
            "target_spread": self._target_spread,
            "max_inventory_pct": self._max_inventory_pct,
            "skew_factor": self._skew_factor,
            "size_pct": self._size_pct,
            "max_position_age_minutes": self._max_position_age,
        }
