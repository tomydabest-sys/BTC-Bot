"""Negative-risk arbitrage across an event's buckets.

If the sum of best-ask prices across the bucket markets of a single event
is < 0.98 (after fees), we can buy YES on the cheapest underpriced buckets
in a weight that's approximately delta-neutral across outcomes. Rarely
fires; high confidence requirement.
"""

from __future__ import annotations

from dataclasses import dataclass

from polybot.data.models import Direction, Signal


@dataclass
class EventBucketsView:
    event_id: str
    station: str
    city: str
    buckets: list[dict]


class NegativeRiskArbStrategy:
    def __init__(self, min_gap_bps: float = 200.0, confidence_min: float = 0.95) -> None:
        self.min_gap_bps = float(min_gap_bps)
        self.confidence_min = float(confidence_min)

    @property
    def name(self) -> str:
        return "negative_risk_arb"

    def evaluate(self, view: EventBucketsView) -> list[Signal]:
        asks = [b["best_ask"] for b in view.buckets if 0 < b["best_ask"] < 1]
        bids = [b["best_bid"] for b in view.buckets if 0 < b["best_bid"] < 1]
        if not asks or not bids:
            return []
        sum_asks = sum(asks)
        sum_bids = sum(bids)

        threshold = self.min_gap_bps / 10000.0
        signals: list[Signal] = []

        # YES side: sum_asks < 1 - threshold → buy the basket
        if sum_asks < 1.0 - threshold:
            gap_bps = (1.0 - sum_asks) * 10000.0
            for b in view.buckets:
                if not (0 < b["best_ask"] < 1):
                    continue
                signals.append(
                    Signal(
                        market_id=b["id"],
                        strategy=self.name,
                        direction=Direction.BUY,
                        outcome="YES",
                        target_price=float(b["best_ask"]),
                        confidence=min(0.99, self.confidence_min + (gap_bps - self.min_gap_bps) / 10000.0),
                        size_pct=0.001,  # tiny per-leg; sized at the orchestrator level
                        reason=f"neg_risk_arb sum_asks={sum_asks:.4f} gap={gap_bps:.0f}bps",
                        metadata={
                            "event_id": view.event_id,
                            "station": view.station,
                            "city": view.city,
                            "token_id": b["token_id_yes"],
                            "edge_bps": gap_bps,
                            "fair_value": 1.0 / max(1, len(view.buckets)),
                            "bucket_low": b["bucket_low"],
                            "bucket_high": b["bucket_high"],
                            "is_arb_leg": True,
                            "arb_basket_size": len(view.buckets),
                        },
                    )
                )
            return signals

        # NO side: sum_bids > 1 + threshold → sell the basket (buy NO)
        if sum_bids > 1.0 + threshold:
            gap_bps = (sum_bids - 1.0) * 10000.0
            for b in view.buckets:
                if not (0 < b["best_bid"] < 1):
                    continue
                signals.append(
                    Signal(
                        market_id=b["id"],
                        strategy=self.name,
                        direction=Direction.SELL,
                        outcome="YES",
                        target_price=float(b["best_bid"]),
                        confidence=min(0.99, self.confidence_min + (gap_bps - self.min_gap_bps) / 10000.0),
                        size_pct=0.001,
                        reason=f"neg_risk_arb sum_bids={sum_bids:.4f} gap={gap_bps:.0f}bps",
                        metadata={
                            "event_id": view.event_id,
                            "station": view.station,
                            "city": view.city,
                            "token_id": b["token_id_no"],
                            "edge_bps": gap_bps,
                            "fair_value": 1.0 / max(1, len(view.buckets)),
                            "bucket_low": b["bucket_low"],
                            "bucket_high": b["bucket_high"],
                            "is_arb_leg": True,
                            "arb_basket_size": len(view.buckets),
                        },
                    )
                )
        return signals
