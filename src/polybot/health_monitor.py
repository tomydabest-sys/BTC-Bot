"""Feed health monitoring — detects silent stalls.

Per the dev.to oracle-lag-sniper post-mortem, the dominant operational failure
in Polymarket bots is *"WebSocket stays alive, ping-pong works, but the upstream
just quietly stops sending data."* This module catches that.

Usage:
    hm = HealthMonitor()
    hm.stamp("binance_btc")           # call from feed callbacks
    if hm.stale("binance_btc", 5):    # check before each cycle
        skip_cycle()

Built-in feed names by convention:
    - "binance_btc"      : Binance WS price feed
    - "polymarket_book"  : Polymarket CLOB book updates
    - "polymarket_trade" : Polymarket trade-stream
    - "scanner"          : Periodic market scanner
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class FeedStatus:
    name: str
    last_ts: float
    age_s: float
    is_stale: bool
    threshold_s: float


class HealthMonitor:
    """Tracks last-seen timestamps for named feeds.

    Thread-safe enough for asyncio (single-event-loop access). Not safe across
    OS threads — wrap with a lock if you need that.
    """

    def __init__(self) -> None:
        self._last_ts: dict[str, float] = {}
        self._thresholds: dict[str, float] = {
            "binance_btc": 5.0,
            "polymarket_book": 10.0,
            "polymarket_trade": 30.0,
            "scanner": 120.0,
        }

    def configure_threshold(self, name: str, max_quiet_s: float) -> None:
        """Set a custom staleness threshold for a feed."""
        self._thresholds[name] = float(max_quiet_s)

    def stamp(self, name: str) -> None:
        """Record that the feed produced data just now."""
        self._last_ts[name] = time.time()

    def stale(self, name: str, max_quiet_s: float | None = None) -> bool:
        """Is the named feed stale?"""
        threshold = max_quiet_s if max_quiet_s is not None else self._thresholds.get(name, 30.0)
        last = self._last_ts.get(name)
        if last is None:
            # Never stamped → considered stale (warming up)
            return True
        return (time.time() - last) > threshold

    def age_s(self, name: str) -> float:
        """How many seconds since the feed was last stamped. Inf if never."""
        last = self._last_ts.get(name)
        if last is None:
            return float("inf")
        return time.time() - last

    def status(self, name: str) -> FeedStatus:
        last = self._last_ts.get(name, 0.0)
        threshold = self._thresholds.get(name, 30.0)
        age = (time.time() - last) if last > 0 else float("inf")
        return FeedStatus(
            name=name,
            last_ts=last,
            age_s=age,
            is_stale=(age > threshold),
            threshold_s=threshold,
        )

    def summary(self) -> dict[str, FeedStatus]:
        """Return a snapshot of all known feed statuses."""
        names = set(self._last_ts) | set(self._thresholds)
        return {n: self.status(n) for n in names}

    def all_healthy(self, *names: str) -> bool:
        """True iff none of the named feeds are stale."""
        return not any(self.stale(n) for n in names)


# Process-wide singleton (optional convenience)
_global_monitor = HealthMonitor()


def get_monitor() -> HealthMonitor:
    """Return the process-wide HealthMonitor singleton."""
    return _global_monitor
