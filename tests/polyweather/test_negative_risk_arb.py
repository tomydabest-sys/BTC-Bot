"""NegativeRiskArbStrategy: fires only when basket prices imply a gap."""

from __future__ import annotations

from polybot.polyweather.strategies.negative_risk_arb import (
    EventBucketsView,
    NegativeRiskArbStrategy,
)


def _buckets(prices):
    return [
        {
            "id": f"b{i}",
            "best_ask": prices[i],
            "best_bid": max(0.001, prices[i] - 0.02),
            "bucket_low": i * 10,
            "bucket_high": (i + 1) * 10,
            "token_id_yes": f"y{i}",
            "token_id_no": f"n{i}",
        }
        for i in range(len(prices))
    ]


def test_sum_below_one_minus_threshold_fires_yes() -> None:
    strat = NegativeRiskArbStrategy(min_gap_bps=200, confidence_min=0.95)
    view = EventBucketsView(event_id="e", station="KLGA", city="NYC",
                            buckets=_buckets([0.10, 0.20, 0.30, 0.30]))
    sigs = strat.evaluate(view)
    assert sigs, "sum_asks=0.90 < 0.98 → should fire"


def test_sum_near_one_no_fire() -> None:
    strat = NegativeRiskArbStrategy(min_gap_bps=200, confidence_min=0.95)
    view = EventBucketsView(event_id="e", station="KLGA", city="NYC",
                            buckets=_buckets([0.10, 0.20, 0.30, 0.40]))
    sigs = strat.evaluate(view)
    # sum_asks 1.00 → not < 0.98 → no fire; bids ~0.92 → not > 1.02 → no fire
    assert sigs == []


def test_sum_above_one_plus_threshold_fires_sell() -> None:
    strat = NegativeRiskArbStrategy(min_gap_bps=200, confidence_min=0.95)
    # bids = ask - 0.02; want sum_bids > 1.02 → asks > 1.04/4 = 0.26 each? Use larger
    buckets = [
        {"id": "b0", "best_ask": 0.40, "best_bid": 0.38, "bucket_low": 0, "bucket_high": 10, "token_id_yes": "y0", "token_id_no": "n0"},
        {"id": "b1", "best_ask": 0.45, "best_bid": 0.42, "bucket_low": 10, "bucket_high": 20, "token_id_yes": "y1", "token_id_no": "n1"},
        {"id": "b2", "best_ask": 0.30, "best_bid": 0.28, "bucket_low": 20, "bucket_high": 30, "token_id_yes": "y2", "token_id_no": "n2"},
    ]
    view = EventBucketsView(event_id="e", station="KLGA", city="NYC", buckets=buckets)
    sigs = strat.evaluate(view)
    assert sigs
    assert all(s.direction.value == "SELL" for s in sigs)
