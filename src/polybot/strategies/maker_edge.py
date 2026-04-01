"""Maker-edge strategy — passive liquidity provision that exploits taker losses.

Based on Jon Becker's prediction-market-analysis research:
- Makers earn positive excess returns at nearly every price point
- Takers systematically lose — they pay the spread and get worse fills
- The maker-taker gap widens during high-volume periods (more uninformed takers)
- Makers who quote inside the spread capture the best edge

This strategy:
1. Only posts LIMIT orders (maker-only, never crosses the spread)
2. Quotes at prices where maker excess returns are highest
3. Skews quotes based on order flow toxicity detection
4. Manages inventory to avoid directional exposure
"""

from __future__ import annotations

import math
from datetime import datetime

from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


# Hours (UTC) with highest uninformed flow → best for makers
# Based on returns_by_hour analysis: late night/early morning US hours
# have more uninformed retail flow, better for passive makers
HIGH_EDGE_HOURS_UTC = {3, 4, 5, 6, 7, 8, 13, 14, 15, 16}


class MakerEdgeStrategy(BaseStrategy):
    """Passive maker strategy that profits from taker flow toxicity patterns."""

    def __init__(
        self,
        min_spread: float = 0.02,
        quote_offset: float = 0.005,
        max_inventory: float = 0.20,
        inventory_skew: float = 0.6,
        size_pct: float = 0.05,
        time_of_day_filter: bool = True,
        volume_boost_threshold: float = 10000.0,
    ) -> None:
        self._min_spread = min_spread
        self._quote_offset = quote_offset
        self._max_inventory = max_inventory
        self._inventory_skew = inventory_skew
        self._size_pct = size_pct
        self._time_filter = time_of_day_filter
        self._volume_boost = volume_boost_threshold
        self._inventory: dict[str, float] = {}

    @property
    def name(self) -> str:
        return "maker_edge"

    def update_inventory(self, market_id: str, delta: float) -> None:
        self._inventory[market_id] = self._inventory.get(market_id, 0) + delta

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        mid = snapshot.orderbook.mid_price
        spread = snapshot.orderbook.spread

        # Need minimum spread to be profitable as maker
        if spread < self._min_spread:
            return None

        # Avoid extreme prices where outcomes are near-certain
        if mid <= 0.03 or mid >= 0.97:
            return None

        # Time-of-day filter: prefer hours with more uninformed flow
        hour_boost = 1.0
        if self._time_filter:
            now_utc = datetime.utcnow().hour
            if now_utc in HIGH_EDGE_HOURS_UTC:
                hour_boost = 1.3  # 30% boost during high-edge hours
            else:
                hour_boost = 0.8  # Still trade, but smaller

        # Volume boost: more volume = more takers = better fills for makers
        vol_boost = 1.0
        if snapshot.market.volume_24h > self._volume_boost:
            vol_boost = min(
                1.0 + math.log10(snapshot.market.volume_24h / self._volume_boost) * 0.3,
                1.5,
            )

        # Inventory management
        net_inv = self._inventory.get(snapshot.market.id, 0.0)
        skew = -net_inv * self._inventory_skew

        # Quote placement: inside the spread for better queue priority
        # but offset enough to ensure profitability
        half_spread = spread / 2
        our_bid = mid - half_spread + self._quote_offset + skew
        our_ask = mid + half_spread - self._quote_offset + skew

        # Clamp
        our_bid = max(0.01, min(our_bid, 0.99))
        our_ask = max(0.01, min(our_ask, 0.99))

        # If inventory is overloaded, only reduce
        if abs(net_inv) > self._max_inventory:
            if net_inv > 0:
                direction = Direction.SELL
                outcome = "No"
                target_price = our_ask
            else:
                direction = Direction.BUY
                outcome = "Yes"
                target_price = our_bid
        else:
            # Detect order flow direction from recent trades
            flow_imbalance = self._estimate_flow_toxicity(snapshot)

            if flow_imbalance > 0.15:
                # Informed buying detected → lean toward selling to them
                direction = Direction.SELL
                outcome = "No"
                target_price = our_ask
            elif flow_imbalance < -0.15:
                # Informed selling → lean toward buying
                direction = Direction.BUY
                outcome = "Yes"
                target_price = our_bid
            else:
                # Balanced flow → prefer the side with more depth (less competition)
                if snapshot.orderbook.bid_depth < snapshot.orderbook.ask_depth:
                    direction = Direction.BUY
                    outcome = "Yes"
                    target_price = our_bid
                else:
                    direction = Direction.SELL
                    outcome = "No"
                    target_price = our_ask

        # Confidence
        spread_edge = spread - self._min_spread
        confidence = min(0.5 + spread_edge / spread * 0.4, 0.9)

        # Size with boosts
        adj_size = self._size_pct * hour_boost * vol_boost

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=target_price,
            confidence=confidence,
            size_pct=adj_size,
            reason=(
                f"Maker edge: spread={spread:.4f}, mid={mid:.4f}, "
                f"inv={net_inv:+.3f}, hour_boost={hour_boost:.1f}, "
                f"vol_boost={vol_boost:.2f}"
            ),
            metadata={
                "bid_quote": our_bid,
                "ask_quote": our_ask,
                "spread": spread,
                "net_inventory": net_inv,
                "hour_boost": hour_boost,
                "vol_boost": vol_boost,
                "is_maker_only": True,
            },
        )

    def _estimate_flow_toxicity(self, snapshot: MarketSnapshot) -> float:
        """Estimate whether recent trade flow is informed (toxic) or uninformed.

        Returns positive if buying pressure is dominant (potential informed buying),
        negative if selling pressure dominates.
        """
        if not snapshot.recent_trades:
            return 0.0

        buy_vol = 0.0
        sell_vol = 0.0
        for trade in snapshot.recent_trades[-20:]:
            if trade.side.value == "BUY":
                buy_vol += trade.size
            else:
                sell_vol += trade.size

        total = buy_vol + sell_vol
        if total == 0:
            return 0.0

        return (buy_vol - sell_vol) / total

    def get_params(self) -> dict:
        return {
            "min_spread": self._min_spread,
            "quote_offset": self._quote_offset,
            "max_inventory": self._max_inventory,
            "inventory_skew": self._inventory_skew,
            "size_pct": self._size_pct,
            "time_of_day_filter": self._time_filter,
            "volume_boost_threshold": self._volume_boost,
        }
