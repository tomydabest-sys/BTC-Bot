"""Unit tests for the feed-driven P1 causal lock (offline, synthetic days).

The lock must be CAUSAL — it may only use obs up to the decision time. These
tests pin the three behaviours that make the backtest honest:
  1. lock after the peak has passed and pick the running-max bucket,
  2. fall back to end-of-day when the day rises monotonically (never declines),
  3. lock at an EARLY false peak and ignore a later climb (so it correctly takes
     the loss when the temperature overtakes the early lock).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analyze import backtest_p1_feed as F  # noqa: E402
from src.common import normalize as N  # noqa: E402

P = {"decline_margin_f": 1.0, "decline_readings": 1}


def _day(rows):
    """rows = [(local_hour, temp_f), ...] -> a day DataFrame (1h spacing)."""
    ts = list(range(1_000_000, 1_000_000 + len(rows) * 3600, 3600))
    return pd.DataFrame({"ts": ts,
                         "temp_f": [t for _, t in rows],
                         "local_hour": [h for h, _ in rows]})


def _buckets(labels):
    return [(lbl, N.parse_bucket_bounds(lbl)) for lbl in labels]


def test_lock_picks_running_max_after_peak():
    b = _buckets(["82-83°F", "84-85°F", "86-87°F"])
    day = _day([(10, 80), (11, 82), (12, 84), (13, 85), (14, 83)])  # decline after hr13
    lock_ts, bucket, run = F._lock(day, b, 13, P)
    assert run == 85.0 and bucket == "84-85°F"
    assert lock_ts == int(day["ts"].iloc[4])


def test_lock_fallback_end_of_day_when_monotone():
    b = _buckets(["84-85°F", "86-87°F"])
    day = _day([(10, 80), (11, 82), (13, 84), (15, 86)])  # never declines
    lock_ts, bucket, run = F._lock(day, b, 13, P)
    assert run == 86.0 and bucket == "86-87°F"
    assert lock_ts == int(day["ts"].iloc[-1])             # end-of-day fallback


def test_lock_takes_early_false_peak_then_later_climb_ignored():
    b = _buckets(["84-85°F", "86-87°F", "88-89°F"])
    day = _day([(12, 84), (13, 85), (14, 83), (15, 88)])  # false peak 85, later 88
    _, bucket, run = F._lock(day, b, 13, P)
    assert run == 85.0 and bucket == "84-85°F"             # ignores the later 88 -> a loss vs an 88-89°F winner


def test_lock_celsius_event():
    b = _buckets(["19°C", "20°C", "21°C"])
    # 68.0°F == 20°C peak, then declines
    day = _day([(11, 64.4), (12, 66.2), (13, 68.0), (14, 64.4)])
    _, bucket, run = F._lock(day, b, 13, P)
    assert bucket == "20°C"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} feed-backtest tests passed")
