"""Primary weather signal — ensemble-model edge.

Edge = ``p_model - p_market``. Fire YES if ``edge > +threshold``, NO if
``edge < -threshold``. Position sized via quarter-Kelly + 1%-of-bankroll
hard cap, plus the first-24h-live $5 cap when live mode is fresh.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from polybot.data.models import Direction, Signal
from polybot.polyweather.data.forecasts.ensemble_blender import EnsembleForecast

logger = structlog.get_logger()


@dataclass
class WeatherMarketView:
    """One bucket's view from the bot's perspective at a given instant."""

    market_id: str
    event_id: str
    station: str
    city: str
    token_id_yes: str
    token_id_no: str
    bucket_low: float
    bucket_high: float
    best_bid: float
    best_ask: float
    horizon_hours: float
    forecast: EnsembleForecast


def _kelly_fraction(p: float, b: float) -> float:
    """Vanilla Kelly for binary outcomes with odds ``b:1``."""
    if b <= 0:
        return 0.0
    f = (p * (b + 1) - 1) / b
    return max(0.0, f)


class WeatherEnsembleStrategy:
    """The 70%-weighted primary strategy."""

    def __init__(
        self,
        edge_threshold_bps: float = 800.0,
        confidence_min: float = 0.55,
        kelly_multiplier: float = 0.25,
        max_position_pct_bankroll: float = 0.01,
        band_min: float = 0.15,
        band_max: float = 0.85,
        longshot_override_mult: float = 3.0,
    ) -> None:
        self.edge_threshold_bps = float(edge_threshold_bps)
        self.confidence_min = float(confidence_min)
        self.kelly_multiplier = float(kelly_multiplier)
        self.max_position_pct = float(max_position_pct_bankroll)
        # Middle-band gate (weather-wallet research headline): the durable edge
        # lives in the uncertain middle, not the cheap longshots. Only buy a
        # share priced in [band_min, band_max]; sub-band longshots need
        # p_win >= longshot_override_mult × price (steep, deliberately rare).
        self.band_min = float(band_min)
        self.band_max = float(band_max)
        self.longshot_override_mult = float(longshot_override_mult)

    @property
    def name(self) -> str:
        return "weather_ensemble"

    def evaluate(self, view: WeatherMarketView) -> Signal | None:
        forecast = view.forecast
        if forecast.confidence < self.confidence_min:
            return None

        mid = (view.best_bid + view.best_ask) / 2.0
        if mid <= 0 or mid >= 1:
            return None

        p_model = forecast.p_bucket
        edge_yes = p_model - view.best_ask  # buying YES
        edge_no = (1.0 - p_model) - (1.0 - view.best_bid)  # buying NO via 1-mid framing
        edge_no = (1.0 - p_model) - (1.0 - view.best_bid)

        threshold = self.edge_threshold_bps / 10000.0
        if abs(edge_yes) < threshold and abs(edge_no) < threshold:
            return None

        if edge_yes >= threshold:
            direction = Direction.BUY
            token_id = view.token_id_yes
            target_price = view.best_ask
            p_win = p_model
            edge_bps = edge_yes * 10000.0
            outcome = "YES"
        elif edge_no >= threshold:
            direction = Direction.BUY
            token_id = view.token_id_no
            target_price = 1.0 - view.best_bid
            p_win = 1.0 - p_model
            edge_bps = edge_no * 10000.0
            outcome = "NO"
        else:
            return None

        if target_price <= 0 or target_price >= 1:
            return None

        # Middle-band gate. ``p_win`` is the model's probability for the share
        # we'd actually buy (YES or NO), so the override is correct on both
        # sides. Paying > band_max for a near-certain share is capital-
        # inefficient (one bad resolution erases ~20 wins); buying < band_min
        # is the longshot tail where the studied wallets bleed.
        if target_price > self.band_max:
            return None
        if target_price < self.band_min and p_win < self.longshot_override_mult * target_price:
            return None

        b = (1.0 - target_price) / target_price
        kelly = _kelly_fraction(p_win, b) * self.kelly_multiplier
        size_pct = min(self.max_position_pct, kelly)

        return Signal(
            market_id=view.market_id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=float(target_price),
            confidence=float(forecast.confidence),
            size_pct=float(size_pct),
            reason=(
                f"ensemble p={p_model:.3f} vs ask={view.best_ask:.3f} edge={edge_bps:.0f}bps"
                f" conf={forecast.confidence:.2f}"
            ),
            metadata={
                "station": view.station,
                "city": view.city,
                "event_id": view.event_id,
                "token_id": token_id,
                "bucket_low": view.bucket_low,
                "bucket_high": view.bucket_high,
                "model_probability": p_model,
                "edge_bps": edge_bps,
                "fair_value": p_model,
                "horizon_hours": view.horizon_hours,
                "model_contributions": forecast.model_contributions,
                "sigma_used": forecast.sigma_used,
            },
        )
