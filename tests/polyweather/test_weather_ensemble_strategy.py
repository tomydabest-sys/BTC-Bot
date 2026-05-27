"""WeatherEnsembleStrategy: positive edge → YES, negative → NO, none if below threshold."""

from __future__ import annotations

from polybot.data.models import Direction
from polybot.polyweather.data.forecasts.ensemble_blender import EnsembleForecast
from polybot.polyweather.strategies.weather_ensemble import (
    WeatherEnsembleStrategy,
    WeatherMarketView,
)


def _view(p_model: float, best_ask: float, best_bid: float, conf: float = 0.9) -> WeatherMarketView:
    forecast = EnsembleForecast(
        p_bucket=p_model,
        confidence=conf,
        bucket_low=73, bucket_high=77,
        horizon_hours=30,
        members_used=31,
        sigma_used=1.0,
    )
    return WeatherMarketView(
        market_id="m1", event_id="e1", station="KLGA", city="New York",
        token_id_yes="tk_y", token_id_no="tk_n",
        bucket_low=73, bucket_high=77,
        best_bid=best_bid, best_ask=best_ask, horizon_hours=30,
        forecast=forecast,
    )


def test_positive_edge_fires_yes() -> None:
    strat = WeatherEnsembleStrategy(edge_threshold_bps=500, confidence_min=0.5)
    sig = strat.evaluate(_view(p_model=0.85, best_ask=0.60, best_bid=0.58))
    assert sig is not None
    assert sig.direction == Direction.BUY
    assert sig.outcome == "YES"
    assert sig.metadata["edge_bps"] >= 500


def test_negative_edge_fires_no() -> None:
    strat = WeatherEnsembleStrategy(edge_threshold_bps=500, confidence_min=0.5)
    sig = strat.evaluate(_view(p_model=0.20, best_ask=0.62, best_bid=0.60))
    assert sig is not None
    assert sig.direction == Direction.BUY
    assert sig.outcome == "NO"


def test_small_edge_below_threshold_no_signal() -> None:
    strat = WeatherEnsembleStrategy(edge_threshold_bps=500, confidence_min=0.5)
    sig = strat.evaluate(_view(p_model=0.61, best_ask=0.60, best_bid=0.58))
    assert sig is None


def test_low_confidence_no_signal() -> None:
    strat = WeatherEnsembleStrategy(edge_threshold_bps=500, confidence_min=0.7)
    sig = strat.evaluate(_view(p_model=0.95, best_ask=0.60, best_bid=0.58, conf=0.4))
    assert sig is None


def test_size_pct_respects_kelly_quarter_cap() -> None:
    strat = WeatherEnsembleStrategy(
        edge_threshold_bps=200, confidence_min=0.5, kelly_multiplier=0.25,
        max_position_pct_bankroll=0.01,
    )
    sig = strat.evaluate(_view(p_model=0.95, best_ask=0.50, best_bid=0.48))
    assert sig is not None
    assert sig.size_pct <= 0.01
