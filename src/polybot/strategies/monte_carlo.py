"""Monte Carlo pricing strategy — simulation-based probability estimation.

Inspired by RohOnChain's hedge fund methodology: run Monte Carlo
simulations to estimate true event probabilities, then trade the gap
between simulated fair value and market price.

Uses volatility clustering (GARCH-like) and recent price dynamics to
generate thousands of paths and compute resolution probabilities.
"""

from __future__ import annotations

import random
import math

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy
from polybot.strategies.market_filter import is_crypto_window_market


class MonteCarloStrategy(BaseStrategy):
    """Uses Monte Carlo simulation to price prediction markets."""

    def __init__(
        self,
        num_simulations: int = 5000,
        min_edge_pct: float = 0.05,
        time_window_minutes: int = 15,
        vol_lookback_seconds: int = 300,
        size_pct: float = 0.05,
        confidence_floor: float = 0.55,
        market_keywords: list[str] | None = None,
    ) -> None:
        self._num_simulations = num_simulations
        self._min_edge_pct = min_edge_pct
        self._time_window = time_window_minutes
        self._vol_lookback = vol_lookback_seconds
        self._size_pct = size_pct
        self._confidence_floor = confidence_floor
        self._market_keywords = market_keywords
        self._exchange_feed: PriceFeedState | None = None

    @property
    def name(self) -> str:
        return "monte_carlo"

    def set_exchange_feed(self, feed: PriceFeedState) -> None:
        self._exchange_feed = feed

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        if not self._exchange_feed or len(self._exchange_feed.ticks) < 30:
            return None

        # Only works on crypto time-window markets
        if not is_crypto_window_market(snapshot.market.question, self._market_keywords):
            return None

        # Get current exchange price and volatility
        current_price = self._exchange_feed.last_price
        if current_price == 0:
            return None

        vol = self._exchange_feed.volatility_window(self._vol_lookback)
        if vol == 0:
            return None

        # Annualized vol → per-minute vol
        # vol is already in price units over the lookback window
        # Convert to per-minute return vol
        vol_per_second = vol / current_price / math.sqrt(self._vol_lookback)
        vol_per_minute = vol_per_second * math.sqrt(60)

        # Detect drift (recent momentum)
        momentum = self._exchange_feed.momentum_score()
        drift_per_minute = momentum / self._time_window  # Spread across window

        # Run Monte Carlo
        prob_up = self._simulate(
            current_price,
            drift_per_minute,
            vol_per_minute,
            self._time_window,
        )

        # Compare to market price
        poly_yes = snapshot.orderbook.mid_price
        edge = prob_up - poly_yes

        if abs(edge) < self._min_edge_pct:
            return None

        if edge > 0:
            # Market underpricing Yes (up)
            direction = Direction.BUY
            outcome = "Yes"
            target_price = snapshot.orderbook.best_ask
        else:
            # Market overpricing Yes → buy No
            direction = Direction.SELL
            outcome = "No"
            target_price = snapshot.orderbook.best_bid

        # Confidence: how strong is the edge relative to uncertainty
        confidence = min(abs(edge) / (self._min_edge_pct * 3), 1.0)
        confidence = max(confidence, self._confidence_floor)

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=target_price,
            confidence=confidence,
            size_pct=self._size_pct * confidence,
            reason=(
                f"MC simulation: P(up)={prob_up:.3f} vs market={poly_yes:.3f}, "
                f"edge={edge:+.3f}"
            ),
            metadata={
                "prob_up": prob_up,
                "prob_down": 1 - prob_up,
                "poly_yes": poly_yes,
                "edge": edge,
                "vol_per_minute": vol_per_minute,
                "drift_per_minute": drift_per_minute,
                "num_simulations": self._num_simulations,
                "current_exchange_price": current_price,
            },
        )

    def _simulate(
        self,
        start_price: float,
        drift: float,
        vol: float,
        minutes: int,
    ) -> float:
        """Run Monte Carlo paths and return probability of price being up."""
        up_count = 0
        for _ in range(self._num_simulations):
            price = start_price
            for _step in range(minutes):
                # Geometric Brownian Motion step
                z = random.gauss(0, 1)
                ret = drift + vol * z
                price *= (1 + ret)
            if price > start_price:
                up_count += 1
        return up_count / self._num_simulations

    def get_params(self) -> dict:
        return {
            "num_simulations": self._num_simulations,
            "min_edge_pct": self._min_edge_pct,
            "time_window_minutes": self._time_window,
            "vol_lookback_seconds": self._vol_lookback,
            "size_pct": self._size_pct,
            "confidence_floor": self._confidence_floor,
        }
