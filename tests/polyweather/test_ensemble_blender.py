"""EnsembleBlender invariants + KLGA fixture check."""

from __future__ import annotations

from polybot.polyweather.data.forecasts.ensemble_blender import (
    EnsembleBlender,
    sigma_for_horizon,
)
from polybot.polyweather.data.forecasts.open_meteo_client import MockOpenMeteoClient


def test_sigma_monotonic_with_horizon() -> None:
    assert sigma_for_horizon(1) <= sigma_for_horizon(24) <= sigma_for_horizon(240)
    assert sigma_for_horizon(6) == 0.8
    assert sigma_for_horizon(240) == 5.5


def test_blend_klga_fixture_high_probability_correct_bucket() -> None:
    import asyncio

    om = MockOpenMeteoClient()
    forecast = asyncio.run(om.forecast(40.7769, -73.8740))
    blender = EnsembleBlender()

    result = blender.blend_open_meteo(
        forecast=forecast,
        nws_points=None,
        bucket_low=73,
        bucket_high=77,
        target_horizon_h=30,
        base_rate=0.42,
    )
    assert 0.0 <= result.p_bucket <= 1.0
    assert 0.0 <= result.confidence <= 1.0
    assert result.p_bucket >= 0.55, f"expected dominant bucket 73-77 ≥0.55, got {result.p_bucket}"


def test_blend_bucket_probabilities_consistent() -> None:
    import asyncio

    om = MockOpenMeteoClient()
    forecast = asyncio.run(om.forecast(40.7769, -73.8740))
    blender = EnsembleBlender()

    buckets = [(-999, 70), (70, 73), (73, 77), (77, 80), (80, 999)]
    p_sum = sum(
        blender.blend_open_meteo(forecast, None, lo, hi, target_horizon_h=30).p_bucket
        for lo, hi in buckets
    )
    assert abs(p_sum - 1.0) < 0.05, f"bucket probabilities should ~sum to 1, got {p_sum}"


def test_blend_confidence_with_ensemble_members() -> None:
    import asyncio

    om = MockOpenMeteoClient()
    forecast = asyncio.run(om.forecast(40.7769, -73.8740))
    blender = EnsembleBlender()
    out = blender.blend_open_meteo(forecast, None, 73, 77, target_horizon_h=30)
    assert out.members_used == 31
    assert out.confidence > 0.0


def test_blend_no_models_returns_zero_prob() -> None:
    blender = EnsembleBlender()
    result = blender.blend(deterministic_models={}, ensemble=None,
                           bucket_low=70, bucket_high=80, target_horizon_h=30)
    assert result.p_bucket == 0.0
    assert result.confidence == 0.0
