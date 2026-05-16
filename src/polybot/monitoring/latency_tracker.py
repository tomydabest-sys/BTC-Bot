"""Cancel/replace round-trip latency tracker.

The single most important operational metric for the maker bot. After the
500ms taker delay was removed (18 Feb 2026), any quote we can't cancel
faster than the next BTC move *will* get adversely selected.

Behaviour:
  * `record(rtt_ms)` from QuoteManager after each cancel/replace cycle.
  * `p95` / `p99` over a rolling window.
  * `should_disable_live()` returns True when p95 has exceeded the kill
    threshold over a sustained window; callers should flip the bot back
    to paper mode and alert.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class LatencyConfig:
    warn_p95_ms: float = 150.0
    kill_p95_ms: float = 200.0
    window_seconds: float = 3600.0
    sustained_breach_seconds: float = 60.0
    min_samples: int = 30


@dataclass
class _Sample:
    rtt_ms: float
    ts: float


class LatencyTracker:
    """Rolling RTT statistics with auto-disable threshold."""

    def __init__(self, config: LatencyConfig | None = None) -> None:
        self._config = config or LatencyConfig()
        self._samples: deque[_Sample] = deque()
        self._breach_started_at: float | None = None

    def record(self, rtt_ms: float, *, now: float | None = None) -> None:
        if rtt_ms < 0 or not math.isfinite(rtt_ms):
            return
        ts = now if now is not None else time.monotonic()
        self._samples.append(_Sample(rtt_ms=rtt_ms, ts=ts))
        self._evict(ts)
        self._update_breach(ts)

    def _evict(self, now: float) -> None:
        cutoff = now - self._config.window_seconds
        while self._samples and self._samples[0].ts < cutoff:
            self._samples.popleft()

    def _percentile(self, p: float) -> float:
        if not self._samples:
            return 0.0
        sorted_ms = sorted(s.rtt_ms for s in self._samples)
        if not sorted_ms:
            return 0.0
        # Nearest-rank percentile; fine for ops monitoring.
        rank = max(0, min(len(sorted_ms) - 1, int(round(p * (len(sorted_ms) - 1)))))
        return sorted_ms[rank]

    @property
    def p50(self) -> float:
        return self._percentile(0.50)

    @property
    def p95(self) -> float:
        return self._percentile(0.95)

    @property
    def p99(self) -> float:
        return self._percentile(0.99)

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def _update_breach(self, now: float) -> None:
        if len(self._samples) < self._config.min_samples:
            self._breach_started_at = None
            return
        p95 = self.p95
        if p95 >= self._config.kill_p95_ms:
            if self._breach_started_at is None:
                self._breach_started_at = now
        else:
            self._breach_started_at = None

    def should_disable_live(self, *, now: float | None = None) -> bool:
        if self._breach_started_at is None:
            return False
        ts = now if now is not None else time.monotonic()
        return (ts - self._breach_started_at) >= self._config.sustained_breach_seconds

    def should_warn(self) -> bool:
        if len(self._samples) < self._config.min_samples:
            return False
        return self.p95 >= self._config.warn_p95_ms

    def snapshot(self) -> dict[str, float | int]:
        return {
            "samples": self.sample_count,
            "p50_ms": round(self.p50, 1),
            "p95_ms": round(self.p95, 1),
            "p99_ms": round(self.p99, 1),
            "breaching": bool(self._breach_started_at is not None),
        }

    def reset(self) -> None:
        self._samples.clear()
        self._breach_started_at = None
