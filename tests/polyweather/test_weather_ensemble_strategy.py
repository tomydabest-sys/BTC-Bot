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


def test_longshot_below_band_is_suppressed() -> None:
    # Edge exists (p 0.25 vs ask 0.10 = 1500 bps) but the share is a sub-15c
    # long-shot and conviction isn't 3x the price → suppressed (research: the
    # tail bleeds).
    strat = WeatherEnsembleStrategy(edge_threshold_bps=500, confidence_min=0.5)
    assert strat.evaluate(_view(p_model=0.25, best_ask=0.10, best_bid=0.08)) is None


def test_longshot_override_allows_high_conviction() -> None:
    # p_win 0.60 >= 3 × 0.10 → the steep override lets a rare high-conviction
    # long-shot through.
    strat = WeatherEnsembleStrategy(edge_threshold_bps=500, confidence_min=0.5)
    sig = strat.evaluate(_view(p_model=0.60, best_ask=0.10, best_bid=0.08))
    assert sig is not None and sig.outcome == "YES"


def test_expensive_favorite_above_band_is_suppressed() -> None:
    # Paying 0.90 for a near-certain share is capital-inefficient → suppressed.
    strat = WeatherEnsembleStrategy(edge_threshold_bps=500, confidence_min=0.5)
    assert strat.evaluate(_view(p_model=0.99, best_ask=0.90, best_bid=0.88)) is None


def test_band_is_configurable_to_harvest_favorites() -> None:
    # Operator can widen the upper bound to harvest the 65-95c favorite band.
    strat = WeatherEnsembleStrategy(edge_threshold_bps=500, confidence_min=0.5, band_max=0.95)
    sig = strat.evaluate(_view(p_model=0.99, best_ask=0.90, best_bid=0.88))
    assert sig is not None and sig.outcome == "YES"


def test_size_pct_respects_kelly_quarter_cap() -> None:
    strat = WeatherEnsembleStrategy(
        edge_threshold_bps=200, confidence_min=0.5, kelly_multiplier=0.25,
        max_position_pct_bankroll=0.01,
    )
    sig = strat.evaluate(_view(p_model=0.95, best_ask=0.50, best_bid=0.48))
    assert sig is not None
    assert sig.size_pct <= 0.01
