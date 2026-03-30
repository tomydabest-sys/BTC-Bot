"""Dual-direction arbitrage — guaranteed profit when Yes + No < $1.

Inspired by the Jane Street India wallet strategy: buy both Yes and No
on a binary market when the combined cost is less than $1. One side
always resolves to $1, guaranteeing profit equal to ($1 - total_cost).

This is the safest strategy with near-zero risk when the spread exists.
"""

from __future__ import annotations

from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy


class DualDirectionArbStrategy(BaseStrategy):
    """Detects and exploits Yes + No < $1 arbitrage opportunities."""

    def __init__(
        self,
        min_profit_pct: float = 0.01,
        max_total_cost: float = 0.99,
        min_liquidity_each_side: float = 50.0,
        size_pct: float = 0.10,
    ) -> None:
        self._min_profit_pct = min_profit_pct
        self._max_total_cost = max_total_cost
        self._min_liquidity = min_liquidity_each_side
        self._size_pct = size_pct

    @property
    def name(self) -> str:
        return "dual_direction_arb"

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        # Need a binary market with exactly 2 outcomes
        if len(snapshot.market.outcomes) != 2 or len(snapshot.market.token_ids) < 2:
            return None

        # Get best ask prices for both sides
        # In Polymarket, we need orderbooks for both Yes and No tokens
        # The snapshot gives us one orderbook — we estimate No price as (1 - Yes_ask)
        yes_ask = snapshot.orderbook.best_ask
        yes_bid = snapshot.orderbook.best_bid

        # For a binary market: No_fair = 1 - Yes_fair
        # Best ask on No ≈ 1 - best_bid on Yes
        no_implied_ask = 1.0 - yes_bid

        total_cost = yes_ask + no_implied_ask

        if total_cost >= self._max_total_cost:
            return None

        profit_per_share = 1.0 - total_cost
        profit_pct = profit_per_share / total_cost

        if profit_pct < self._min_profit_pct:
            return None

        # Check there's enough liquidity to execute
        ask_depth = snapshot.orderbook.ask_depth
        bid_depth = snapshot.orderbook.bid_depth
        if ask_depth < self._min_liquidity or bid_depth < self._min_liquidity:
            return None

        # This strategy buys both sides. We signal BUY Yes (the execution
        # engine would need to also buy No in a separate order — we encode
        # this in metadata for the bot to handle).
        confidence = min(profit_pct / (self._min_profit_pct * 5), 1.0)
        confidence = max(confidence, 0.7)  # High floor since this is near-riskless

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=Direction.BUY,
            outcome="Yes",
            target_price=yes_ask,
            confidence=confidence,
            size_pct=self._size_pct,
            reason=(
                f"Dual-direction arb: Yes@{yes_ask:.4f} + No@{no_implied_ask:.4f} "
                f"= {total_cost:.4f} (profit {profit_pct:.2%})"
            ),
            metadata={
                "yes_ask": yes_ask,
                "no_implied_ask": no_implied_ask,
                "total_cost": total_cost,
                "profit_per_share": profit_per_share,
                "profit_pct": profit_pct,
                "is_dual_direction": True,
                "no_token_id": snapshot.market.token_ids[1]
                if len(snapshot.market.token_ids) > 1
                else "",
            },
        )

    def get_params(self) -> dict:
        return {
            "min_profit_pct": self._min_profit_pct,
            "max_total_cost": self._max_total_cost,
            "min_liquidity_each_side": self._min_liquidity,
            "size_pct": self._size_pct,
        }
