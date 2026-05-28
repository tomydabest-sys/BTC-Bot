"""Point-market + Celsius support in the EnsembleBlender.

Real Polymarket weather markets are point-style ("26°C"), not range-style
("76-77°F"). The blender used to return p_bucket=0 for point markets
because cdf(high) - cdf(low) == 0 when low == high. These tests pin the
±0.5° tolerance and the °C/°F handling.
"""

from __future__ import annotations

from polybot.polyweather.data.forecasts.ensemble_blender import (
    c_to_f,
    f_to_c,
    p_bucket_gaussian,
)


def test_point_market_returns_nonzero_probability() -> None:
    # mu=75°F, point market at 75°F → P ≈ 50% (centred Gaussian on bucket)
    p = p_bucket_gaussian(mu=75.0, sigma=1.0, low=75.0, high=75.0)
    assert 0.30 < p < 0.50


def test_point_market_off_centre_low_probability() -> None:
    # mu=70°F, point at 80°F → far in the tail
    p = p_bucket_gaussian(mu=70.0, sigma=1.0, low=80.0, high=80.0)
    assert p < 0.01


def test_range_market_unchanged_behaviour() -> None:
    p = p_bucket_gaussian(mu=75.0, sigma=2.0, low=73.0, high=77.0)
    assert 0.65 < p < 0.75  # roughly P(|Z| < 1) for N(75, 2)


def test_unit_helpers_roundtrip() -> None:
    assert abs(c_to_f(0.0) - 32.0) < 1e-9
    assert abs(c_to_f(100.0) - 212.0) < 1e-9
    assert abs(f_to_c(32.0) - 0.0) < 1e-9
    assert abs(f_to_c(212.0) - 100.0) < 1e-9


def test_celsius_bucket_via_full_blender() -> None:
    """A Celsius point market with a matching forecast should fire ~50%."""
    import asyncio

    from polybot.polyweather.data.forecasts.ensemble_blender import EnsembleBlender
    from polybot.polyweather.data.forecasts.open_meteo_client import MockOpenMeteoClient

    om = MockOpenMeteoClient()
    forecast = asyncio.run(om.forecast(40.7769, -73.8740))  # KLGA, mu≈75°F = ~24°C
    blender = EnsembleBlender()

    # Bucket: 24°C point in Celsius
    result = blender.blend_open_meteo(
        forecast=forecast,
        nws_points=None,
        bucket_low=24.0,
        bucket_high=24.0,
        target_horizon_h=30,
        bucket_unit="C",
    )
    assert 0.0 < result.p_bucket < 1.0
    assert 0.10 < result.p_bucket < 0.70


def test_celsius_bucket_far_off_low_probability() -> None:
    """A 0°C bucket against a 75°F (~24°C) forecast should be ~0."""
    import asyncio

    from polybot.polyweather.data.forecasts.ensemble_blender import EnsembleBlender
    from polybot.polyweather.data.forecasts.open_meteo_client import MockOpenMeteoClient

    om = MockOpenMeteoClient()
    forecast = asyncio.run(om.forecast(40.7769, -73.8740))
    blender = EnsembleBlender()

    result = blender.blend_open_meteo(
        forecast=forecast,
        nws_points=None,
        bucket_low=0.0,
        bucket_high=0.0,
        target_horizon_h=30,
        bucket_unit="C",
    )
    assert result.p_bucket < 0.01
