"""Late-window mean reversion (2h ≤ ttr ≤ 6h).

Fires when our model strongly believes a bucket will resolve YES but the
market hasn't priced it that way yet.
"""

from __future__ import annotations

from polybot.data.models import Direction, Signal
from polybot.polyweather.strategies.weather_ensemble import WeatherMarketView


class ResolutionMeanRevStrategy:
    def __init__(self, edge_threshold_bps: float = 500.0, confidence_min: float = 0.85) -> None:
        self.edge_threshold_bps = float(edge_threshold_bps)
        self.confidence_min = float(confidence_min)

    @property
    def name(self) -> str:
        return "resolution_meanrev"

    def evaluate(self, view: WeatherMarketView) -> Signal | None:
        if not (2.0 <= view.horizon_hours <= 6.0):
            return None
        if view.forecast.confidence < self.confidence_min:
            return None
        if view.forecast.p_bucket <= 0.95:
            return None
        if not (0 < view.best_ask < 0.92):
            return None
        edge = view.forecast.p_bucket - view.best_ask
        if edge < self.edge_threshold_bps / 10000.0:
            return None
        confidence = min(view.forecast.p_bucket, 1.0 - (view.best_ask - 0.85) / 0.10)
        confidence = max(0.0, min(1.0, confidence))
        return Signal(
            market_id=view.market_id,
            strategy=self.name,
            direction=Direction.BUY,
            outcome="YES",
            target_price=float(view.best_ask),
            confidence=float(confidence),
            size_pct=0.003,
            reason=f"resolution_meanrev p={view.forecast.p_bucket:.3f} ask={view.best_ask:.3f}",
            metadata={
                "station": view.station,
                "city": view.city,
                "event_id": view.event_id,
                "token_id": view.token_id_yes,
                "bucket_low": view.bucket_low,
                "bucket_high": view.bucket_high,
                "model_probability": view.forecast.p_bucket,
                "fair_value": view.forecast.p_bucket,
                "edge_bps": edge * 10000.0,
                "horizon_hours": view.horizon_hours,
            },
        )
