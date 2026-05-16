"""Tests for the cancel/replace latency tracker."""

from __future__ import annotations

from polybot.monitoring.latency_tracker import LatencyConfig, LatencyTracker


def test_percentiles_with_known_distribution():
    cfg = LatencyConfig(min_samples=5)
    t = LatencyTracker(cfg)
    for ms in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        t.record(ms, now=1000.0)
    assert t.p50 == 50
    assert t.p95 == 100
    assert t.sample_count == 10


def test_window_eviction():
    cfg = LatencyConfig(window_seconds=10.0, min_samples=1)
    t = LatencyTracker(cfg)
    t.record(50, now=0.0)
    t.record(60, now=5.0)
    t.record(70, now=12.0)  # evicts only the first (cutoff = 2.0)
    assert t.sample_count == 2
    assert t.p50 in (60, 70)
    # Long jump past the window evicts both old samples.
    t.record(80, now=25.0)
    assert t.sample_count == 1


def test_no_kill_below_min_samples():
    cfg = LatencyConfig(kill_p95_ms=10, min_samples=10, sustained_breach_seconds=0.0)
    t = LatencyTracker(cfg)
    for _ in range(5):
        t.record(1000, now=0.0)
    assert t.should_disable_live() is False


def test_kill_requires_sustained_breach():
    cfg = LatencyConfig(
        warn_p95_ms=50, kill_p95_ms=100,
        min_samples=5, sustained_breach_seconds=30.0,
        window_seconds=600.0,
    )
    t = LatencyTracker(cfg)
    for _ in range(10):
        t.record(500, now=0.0)
    # Just above threshold but not yet sustained.
    assert t.should_disable_live(now=10.0) is False
    # After 30s sustained, should trip.
    t.record(500, now=31.0)
    assert t.should_disable_live(now=31.0) is True


def test_kill_resets_when_p95_drops():
    cfg = LatencyConfig(
        warn_p95_ms=50, kill_p95_ms=100,
        min_samples=5, sustained_breach_seconds=10.0,
        window_seconds=20.0,  # short so old breaching samples evict
    )
    t = LatencyTracker(cfg)
    for _ in range(10):
        t.record(500, now=0.0)  # breaching
    assert t.should_disable_live(now=5.0) is False  # not yet sustained
    # Wait long enough for the breaching samples to evict, then add fast ones.
    for _ in range(100):
        t.record(10, now=30.0)
    assert t.should_disable_live(now=30.0) is False


def test_snapshot_shape():
    t = LatencyTracker()
    t.record(50, now=0.0)
    snap = t.snapshot()
    assert {"samples", "p50_ms", "p95_ms", "p99_ms", "breaching"} <= snap.keys()


def test_ignores_invalid_input():
    t = LatencyTracker()
    t.record(-5, now=0.0)
    t.record(float("nan"), now=0.0)
    assert t.sample_count == 0
