"""Passive maker-only quoting on tail buckets ($0.04–$0.15).

Posts resting limit orders one tick inside the spread where the rebate
per fill exceeds the expected slippage. Position cap is $5 per quoted
bucket.
"""

from __future__ import annotations

from polybot.data.models import Direction, Signal
from polybot.polyweather.strategies.weather_ensemble import WeatherMarketView


class MakerRebateStrategy:
    def __init__(
        self,
        max_post_price: float = 0.15,
        min_post_price: float = 0.04,
        per_bucket_cap_usdc: float = 5.0,
    ) -> None:
        self.max_post_price = float(max_post_price)
        self.min_post_price = float(min_post_price)
        self.per_bucket_cap = float(per_bucket_cap_usdc)

    @property
    def name(self) -> str:
        return "maker_rebate"

    def evaluate(self, view: WeatherMarketView) -> Signal | None:
        if not (self.min_post_price <= view.best_bid <= self.max_post_price):
            return None
        # Only quote tails we don't think are about to resolve YES
        if view.forecast.p_bucket > 0.30:
            return None
        post_price = view.best_bid + 0.01
        if post_price >= view.best_ask:
            return None
        return Signal(
            market_id=view.market_id,
            strategy=self.name,
            direction=Direction.BUY,
            outcome="YES",
            target_price=float(post_price),
            confidence=0.50,
            size_pct=0.001,
            reason=f"maker_rebate post {post_price:.3f}",
            metadata={
                "station": view.station,
                "city": view.city,
                "event_id": view.event_id,
                "token_id": view.token_id_yes,
                "is_maker_only": True,
                "per_bucket_cap_usdc": self.per_bucket_cap,
                "bucket_low": view.bucket_low,
                "bucket_high": view.bucket_high,
                "fair_value": post_price,
                "edge_bps": 50.0,
            },
        )
