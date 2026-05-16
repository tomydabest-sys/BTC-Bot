"""CLOB V2 rate limits + a small token-bucket limiter.

Constants reflect the published per-10-second / per-10-minute caps for the
V2 endpoints. Cloudflare throttles (queues) before rejecting outright, so
exceeding these silently *slows* requests rather than 429-ing — that's why
we self-police rather than waiting for a status code.

Usage:
    limiter = MultiBucketLimiter.from_defaults()
    await limiter.acquire("post_order")        # blocks if window saturated
    await limiter.acquire("delete_order")
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

# ─────────────────────────────────────────────────────────────────────────────
#  Published caps
# ─────────────────────────────────────────────────────────────────────────────

RATE_LIMITS: dict[str, dict[str, int]] = {
    "rest_general":    {"per_10s": 15_000},
    "clob_general":    {"per_10s": 9_000},
    "post_order":      {"per_10s": 3_500, "per_10min": 36_000},
    "delete_order":    {"per_10s": 3_000, "per_10min": 30_000},
    "batch":           {"per_10s": 1_000, "per_10min": 15_000},
    "gamma":           {"per_10s": 4_000},
    "data_api":        {"per_10s": 1_000},
}

# Constants that are NOT per-second caps but operational ceilings:
WS_SUBSCRIPTIONS_PER_CONNECTION = 500
BATCH_ORDER_MAX = 15  # V2 raised from 5


# ─────────────────────────────────────────────────────────────────────────────
#  Token bucket
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class _Bucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    last_refill: float

    def take(self, now: float) -> float:
        """Return seconds the caller must sleep before they hold a token.

        Mutates the bucket: if a token is immediately available the caller
        is debited one and the returned wait is 0.
        """
        elapsed = max(0.0, now - self.last_refill)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        deficit = 1.0 - self.tokens
        wait = deficit / self.refill_per_sec if self.refill_per_sec > 0 else 60.0
        # Pre-debit so concurrent callers queue rather than all racing in.
        self.tokens -= 1.0
        return wait


class TokenBucket:
    """Single-window async token bucket. Coalesces concurrent waits."""

    def __init__(self, capacity: int, window_seconds: float) -> None:
        if capacity <= 0 or window_seconds <= 0:
            raise ValueError("capacity and window_seconds must be > 0")
        self._bucket = _Bucket(
            capacity=float(capacity),
            refill_per_sec=float(capacity) / float(window_seconds),
            tokens=float(capacity),
            last_refill=time.monotonic(),
        )
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._bucket.take(time.monotonic())
        if wait > 0:
            await asyncio.sleep(wait)

    @property
    def tokens_available(self) -> float:
        return max(0.0, self._bucket.tokens)


class MultiBucketLimiter:
    """Groups one or more token buckets per endpoint category.

    A single endpoint can be constrained by both a per-10s burst limit and
    a per-10min sustained limit; calling `acquire(name)` waits on whichever
    is binding.
    """

    def __init__(self, buckets: dict[str, list[TokenBucket]]) -> None:
        self._buckets = buckets

    @classmethod
    def from_defaults(cls) -> MultiBucketLimiter:
        buckets: dict[str, list[TokenBucket]] = {}
        for name, limits in RATE_LIMITS.items():
            group: list[TokenBucket] = []
            if "per_10s" in limits:
                group.append(TokenBucket(limits["per_10s"], 10.0))
            if "per_10min" in limits:
                group.append(TokenBucket(limits["per_10min"], 600.0))
            if group:
                buckets[name] = group
        return cls(buckets)

    async def acquire(self, name: str) -> None:
        group = self._buckets.get(name)
        if not group:
            return  # unknown endpoint → don't block
        # Acquire serially; per-10min limit dominates only when sustained.
        for bucket in group:
            await bucket.acquire()

    def snapshot(self) -> dict[str, list[float]]:
        return {
            name: [round(b.tokens_available, 1) for b in group]
            for name, group in self._buckets.items()
        }
