"""Multi-model ensemble blender — heart of the weather signal.

Pipeline:
  1. For each model, treat forecast as Gaussian ``N(μ, σ)`` where σ scales
     linearly from 0.8°F at 6h → 5.5°F at 240h.
  2. Compute ``P_model(T_low ≤ T ≤ T_high)`` via ``norm.cdf``.
  3. Blend models with weights (ECMWF=0.30, GFS=0.25, UKMO=0.20, GEFS=0.25).
  4. Bayesian-update against climatology with weight ``min(0.3, σ/10)``.
  5. Confidence = ``1 - ensemble_member_disagreement_pct``.

Returns ``EnsembleForecast(p_bucket, confidence, model_contributions)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from scipy.stats import norm

from polybot.polyweather.data.forecasts.nws_client import ForecastPoint
from polybot.polyweather.data.forecasts.open_meteo_client import (
    EnsembleMembers,
    OpenMeteoForecast,
)

logger = structlog.get_logger()


MODEL_WEIGHTS = {
    "ecmwf_ifs04": 0.30,
    "gfs_seamless": 0.25,
    "ukmo_seamless": 0.20,
    "gefs": 0.25,
    "NWS": 0.25,  # treated as a GFS-equivalent if all we have
}

SIGMA_AT_6H = 0.8
SIGMA_AT_240H = 5.5


def sigma_for_horizon(horizon_hours: float) -> float:
    """Linear interpolation 0.8°F at 6h to 5.5°F at 240h, clamped."""
    if horizon_hours <= 6:
        return SIGMA_AT_6H
    if horizon_hours >= 240:
        return SIGMA_AT_240H
    frac = (horizon_hours - 6) / (240 - 6)
    return SIGMA_AT_6H + frac * (SIGMA_AT_240H - SIGMA_AT_6H)


def p_bucket_gaussian(mu: float, sigma: float, low: float, high: float) -> float:
    if sigma <= 0:
        return 1.0 if low <= mu <= high else 0.0
    return float(norm.cdf(high, loc=mu, scale=sigma) - norm.cdf(low, loc=mu, scale=sigma))


@dataclass
class EnsembleForecast:
    p_bucket: float
    confidence: float
    bucket_low: float
    bucket_high: float
    horizon_hours: float
    model_contributions: dict[str, float] = field(default_factory=dict)
    members_used: int = 0
    sigma_used: float = 0.0
    base_rate_weight: float = 0.0
    base_rate_used: float | None = None


def ensemble_disagreement_pct(
    members: list[float], bucket_low: float, bucket_high: float
) -> float:
    if not members:
        return 0.0
    inside = sum(1 for x in members if bucket_low <= x <= bucket_high)
    p_in = inside / len(members)
    # Disagreement: distance from a sharp consensus (0 or 1)
    return 1.0 - abs(p_in - 0.5) * 2.0


@dataclass
class ModelForecast:
    """Normalised input row for the blender."""

    name: str
    predicted_temp_f: float
    horizon_hours: float


def _select_for_horizon(
    points: list[ForecastPoint], target_horizon_h: float
) -> ForecastPoint | None:
    if not points:
        return None
    return min(points, key=lambda p: abs(p.horizon_hours - target_horizon_h))


class EnsembleBlender:
    def __init__(self, model_weights: dict[str, float] | None = None) -> None:
        self._weights = dict(model_weights or MODEL_WEIGHTS)

    def blend(
        self,
        deterministic_models: dict[str, list[ForecastPoint]],
        ensemble: EnsembleMembers | None,
        bucket_low: float,
        bucket_high: float,
        target_horizon_h: float,
        base_rate: float | None = None,
    ) -> EnsembleForecast:
        contributions: dict[str, float] = {}
        weighted_sum = 0.0
        weight_total = 0.0
        sigma = sigma_for_horizon(target_horizon_h)

        for name, points in deterministic_models.items():
            chosen = _select_for_horizon(points, target_horizon_h)
            if chosen is None:
                continue
            w = self._weights.get(name, self._weights.get(name.split("/")[-1], 0.20))
            p = p_bucket_gaussian(chosen.predicted_temp_f, sigma, bucket_low, bucket_high)
            contributions[name] = p
            weighted_sum += w * p
            weight_total += w

        members_used = 0
        if ensemble is not None and ensemble.members:
            inside = sum(1 for x in ensemble.members if bucket_low <= x <= bucket_high)
            p_ens = inside / len(ensemble.members)
            w_ens = self._weights.get("gefs", 0.25)
            contributions["gefs_ensemble"] = p_ens
            weighted_sum += w_ens * p_ens
            weight_total += w_ens
            members_used = len(ensemble.members)

        if weight_total == 0:
            return EnsembleForecast(
                p_bucket=0.0,
                confidence=0.0,
                bucket_low=bucket_low,
                bucket_high=bucket_high,
                horizon_hours=target_horizon_h,
                model_contributions=contributions,
                members_used=0,
                sigma_used=sigma,
            )

        p_model = weighted_sum / weight_total

        # Bayesian-update against climatology
        base_rate_weight = 0.0
        if base_rate is not None:
            base_rate_weight = min(0.3, sigma / 10.0)
            p_blended = (1 - base_rate_weight) * p_model + base_rate_weight * base_rate
        else:
            p_blended = p_model

        # Confidence: 1 - ensemble disagreement (if we have ensemble), else
        # derived from σ vs bucket width
        if ensemble is not None and ensemble.members:
            inside = sum(1 for x in ensemble.members if bucket_low <= x <= bucket_high)
            p_in = inside / len(ensemble.members)
            confidence = 1.0 - 2.0 * min(p_in, 1.0 - p_in)
        else:
            width = max(0.001, bucket_high - bucket_low)
            confidence = max(0.0, min(1.0, 1.0 - sigma / max(width, sigma)))

        return EnsembleForecast(
            p_bucket=max(0.0, min(1.0, p_blended)),
            confidence=max(0.0, min(1.0, confidence)),
            bucket_low=bucket_low,
            bucket_high=bucket_high,
            horizon_hours=target_horizon_h,
            model_contributions=contributions,
            members_used=members_used,
            sigma_used=sigma,
            base_rate_weight=base_rate_weight,
            base_rate_used=base_rate,
        )

    def blend_open_meteo(
        self,
        forecast: OpenMeteoForecast,
        nws_points: list[ForecastPoint] | None,
        bucket_low: float,
        bucket_high: float,
        target_horizon_h: float,
        base_rate: float | None = None,
    ) -> EnsembleForecast:
        deterministic = dict(forecast.deterministic)
        if nws_points:
            deterministic["NWS"] = nws_points
        return self.blend(
            deterministic_models=deterministic,
            ensemble=forecast.ensemble,
            bucket_low=bucket_low,
            bucket_high=bucket_high,
            target_horizon_h=target_horizon_h,
            base_rate=base_rate,
        )
