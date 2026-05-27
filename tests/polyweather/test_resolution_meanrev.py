"""ResolutionMeanRevStrategy: late-window, high-confidence only."""

from __future__ import annotations

from polybot.polyweather.data.forecasts.ensemble_blender import EnsembleForecast
from polybot.polyweather.strategies.resolution_meanrev import ResolutionMeanRevStrategy
from polybot.polyweather.strategies.weather_ensemble import WeatherMarketView


def _view(p_model: float, ask: float, horizon: float, conf: float = 0.9) -> WeatherMarketView:
    f = EnsembleForecast(p_bucket=p_model, confidence=conf,
                         bucket_low=70, bucket_high=80, horizon_hours=horizon,
                         members_used=31, sigma_used=1.0)
    return WeatherMarketView(
        market_id="m", event_id="e", station="KLGA", city="NYC",
        token_id_yes="y", token_id_no="n", bucket_low=70, bucket_high=80,
        best_bid=ask - 0.02, best_ask=ask, horizon_hours=horizon, forecast=f,
    )


def test_fires_in_late_window_with_high_conviction() -> None:
    strat = ResolutionMeanRevStrategy()
    sig = strat.evaluate(_view(p_model=0.97, ask=0.85, horizon=4))
    assert sig is not None
    assert sig.strategy == "resolution_meanrev"


def test_no_fire_outside_window() -> None:
    strat = ResolutionMeanRevStrategy()
    assert strat.evaluate(_view(p_model=0.97, ask=0.85, horizon=24)) is None
    assert strat.evaluate(_view(p_model=0.97, ask=0.85, horizon=1)) is None


def test_no_fire_when_market_already_pricing_it() -> None:
    strat = ResolutionMeanRevStrategy()
    assert strat.evaluate(_view(p_model=0.97, ask=0.95, horizon=4)) is None
