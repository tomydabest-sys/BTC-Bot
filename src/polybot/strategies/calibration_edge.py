"""Calibration-edge strategy — exploits systematic mispricing at extreme prices.

Based on Jon Becker's prediction-market-analysis research (72.1M trades, $18.26B volume):
- Longshot bias: contracts at 1-20c are overpriced — buyers lose 60%+ of their money
- At 5c, actual win rate is 4.18% vs 5% implied (-16.36% mispricing)
- Contracts at 80-99c are underpriced favorites that win more than implied
- NO outperforms YES at 69/99 price levels ("optimism tax")
- YES longshots underperform NO longshots by up to 64 percentage points
- Sports/Entertainment have largest inefficiencies; Finance is nearly efficient

This strategy:
1. Fades longshots: buys NO when YES price is very low (YES is overpriced)
2. Backs near-certainties: buys YES when price is high (favorites underpriced)
3. Always prefers NO over YES at equal edge levels (optimism tax)
4. Avoids Finance category (nearly efficient, no edge)
5. Boosts size for Sports/Entertainment (highest retail flow, largest edge)
"""

from __future__ import annotations

from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


# Empirical mispricing by price bucket (from Becker 2026 research)
# Negative = YES overpriced (longshot bias) → fade YES, buy NO
# Positive = YES underpriced (favorite bias) → buy YES
MISPRICING_MAP: dict[tuple[int, int], float] = {
    (1, 5): -0.08,      # Worst longshots: lose 60%+ of money. Huge fade.
    (6, 10): -0.05,     # Still heavily overpriced YES
    (11, 15): -0.03,    # Moderate longshot bias
    (16, 20): -0.02,    # Mild longshot bias
    (80, 85): +0.015,   # Near-certainties slightly underpriced YES
    (86, 90): +0.02,    # Moderate favorite underpricing
    (91, 95): +0.03,    # Strong favorite underpricing
    (96, 99): +0.04,    # Best edge: almost-certain outcomes underpriced
}

# YES/NO asymmetry: NO outperforms YES by this much at each bucket
# At equal edge, always lean NO (the "optimism tax")
NO_BIAS_BONUS: dict[tuple[int, int], float] = {
    (1, 5): +0.02,      # NO massively outperforms YES here
    (6, 10): +0.015,
    (11, 15): +0.01,
    (16, 20): +0.005,
    (80, 85): +0.003,
    (86, 90): +0.003,
    (91, 95): +0.005,   # At 91-99c, NO is the longshot — but still outperforms
    (96, 99): +0.005,
}

# Category edge multipliers (from maker-taker gap analysis)
# Higher = more retail flow = more mispricing = bigger edge
CATEGORY_MULTIPLIERS: dict[str, float] = {
    "sports": 1.4,
    "entertainment": 1.3,
    "media": 1.3,
    "world": 1.2,
    "politics": 1.1,
    "crypto": 1.0,
    "science": 1.0,
    "finance": 0.3,  # Nearly efficient — minimal edge
}


class CalibrationEdgeStrategy(BaseStrategy):
    """Exploits systematic mispricing at extreme price ranges."""

    def __init__(
        self,
        min_edge: float = 0.015,
        size_pct: float = 0.06,
        min_volume_24h: float = 5000.0,
        min_liquidity_depth: float = 100.0,
        avoid_range_low: int = 21,
        avoid_range_high: int = 79,
    ) -> None:
        self._min_edge = min_edge
        self._size_pct = size_pct
        self._min_volume_24h = min_volume_24h
        self._min_liquidity_depth = min_liquidity_depth
        self._avoid_low = avoid_range_low
        self._avoid_high = avoid_range_high

    @property
    def name(self) -> str:
        return "calibration_edge"

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        mid = snapshot.orderbook.mid_price
        if mid <= 0 or mid >= 1:
            return None

        price_cents = int(mid * 100)

        # Skip the well-calibrated middle range (20-80c)
        if self._avoid_low <= price_cents <= self._avoid_high:
            return None

        # Volume filter — need active markets
        if snapshot.market.volume_24h < self._min_volume_24h:
            return None

        # Liquidity filter
        total_depth = snapshot.orderbook.bid_depth + snapshot.orderbook.ask_depth
        if total_depth < self._min_liquidity_depth:
            return None

        # Category multiplier — avoid finance (efficient), boost sports/entertainment
        cat_mult = self._get_category_multiplier(snapshot.market.category)
        if cat_mult < 0.5:
            return None  # Skip nearly-efficient markets (e.g. finance)

        # Find applicable mispricing
        edge = self._lookup_edge(price_cents)
        no_bonus = self._lookup_no_bonus(price_cents)

        # Apply category scaling
        edge *= cat_mult

        if abs(edge) < self._min_edge:
            return None

        if edge < 0:
            # YES is overpriced (longshot bias) → BUY No
            # Add NO bonus (optimism tax makes NO even more profitable)
            effective_edge = abs(edge) + no_bonus
            direction = Direction.SELL
            outcome = "No"
            target_price = snapshot.orderbook.best_bid
            reason = (
                f"Longshot bias: YES@{price_cents}c overpriced "
                f"~{effective_edge*100:.1f}pp (cat={snapshot.market.category})"
            )
        else:
            # YES underpriced (favorite) → BUY Yes
            direction = Direction.BUY
            outcome = "Yes"
            target_price = snapshot.orderbook.best_ask
            effective_edge = edge
            reason = (
                f"Favorite underpriced: YES@{price_cents}c "
                f"edge ~{effective_edge*100:.1f}pp (cat={snapshot.market.category})"
            )

        # Confidence scales with edge magnitude
        confidence = min(effective_edge / (self._min_edge * 3), 0.95)
        confidence = max(confidence, 0.55)

        # Scale position with edge strength and category
        edge_multiplier = effective_edge / self._min_edge
        adj_size = self._size_pct * min(edge_multiplier, 2.5) * cat_mult

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=target_price,
            confidence=confidence,
            size_pct=adj_size,
            reason=reason,
            metadata={
                "price_cents": price_cents,
                "raw_edge": edge,
                "no_bonus": no_bonus,
                "effective_edge": effective_edge,
                "category": snapshot.market.category,
                "category_multiplier": cat_mult,
                "volume_24h": snapshot.market.volume_24h,
                "liquidity_depth": total_depth,
                "is_maker_only": True,  # Always use limit orders
            },
        )

    def _lookup_edge(self, price_cents: int) -> float:
        for (lo, hi), edge in MISPRICING_MAP.items():
            if lo <= price_cents <= hi:
                return edge
        return 0.0

    def _lookup_no_bonus(self, price_cents: int) -> float:
        for (lo, hi), bonus in NO_BIAS_BONUS.items():
            if lo <= price_cents <= hi:
                return bonus
        return 0.0

    def _get_category_multiplier(self, category: str) -> float:
        cat_lower = category.lower().strip() if category else ""
        for key, mult in CATEGORY_MULTIPLIERS.items():
            if key in cat_lower:
                return mult
        return 1.0  # Default for unknown categories

    def get_params(self) -> dict:
        return {
            "min_edge": self._min_edge,
            "size_pct": self._size_pct,
            "min_volume_24h": self._min_volume_24h,
            "min_liquidity_depth": self._min_liquidity_depth,
            "avoid_range": f"{self._avoid_low}-{self._avoid_high}",
        }
