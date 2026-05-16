"""Tests for the V2 rate-limit token bucket."""

from __future__ import annotations

import time

import pytest

from polybot.api.rate_limits import (
    BATCH_ORDER_MAX,
    RATE_LIMITS,
    MultiBucketLimiter,
    TokenBucket,
)


def test_rate_limit_constants_present():
    for required in ("post_order", "delete_order", "batch", "clob_general"):
        assert required in RATE_LIMITS
    assert RATE_LIMITS["post_order"]["per_10s"] == 3_500
    assert RATE_LIMITS["batch"]["per_10s"] == 1_000
    assert BATCH_ORDER_MAX == 15


@pytest.mark.asyncio
async def test_token_bucket_immediate_grants_when_capacity_available():
    bucket = TokenBucket(capacity=5, window_seconds=10.0)
    t0 = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.05, f"five immediate grants took {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_token_bucket_blocks_when_empty():
    bucket = TokenBucket(capacity=2, window_seconds=1.0)
    await bucket.acquire()
    await bucket.acquire()
    t0 = time.monotonic()
    await bucket.acquire()
    elapsed = time.monotonic() - t0
    # 2 tokens / sec → next refill in ~0.5s
    assert 0.3 < elapsed < 1.0, f"expected ~0.5s wait, got {elapsed:.3f}"


@pytest.mark.asyncio
async def test_token_bucket_rejects_bad_params():
    with pytest.raises(ValueError):
        TokenBucket(capacity=0, window_seconds=1.0)
    with pytest.raises(ValueError):
        TokenBucket(capacity=10, window_seconds=0.0)


@pytest.mark.asyncio
async def test_multibucket_limiter_acquires_each_window():
    limiter = MultiBucketLimiter.from_defaults()
    # Should not block on a fresh limiter.
    t0 = time.monotonic()
    for _ in range(50):
        await limiter.acquire("post_order")
    assert (time.monotonic() - t0) < 0.2


@pytest.mark.asyncio
async def test_multibucket_unknown_key_is_no_op():
    limiter = MultiBucketLimiter.from_defaults()
    await limiter.acquire("does_not_exist")  # must not raise


def test_multibucket_snapshot_shape():
    limiter = MultiBucketLimiter.from_defaults()
    snap = limiter.snapshot()
    assert "post_order" in snap
    # post_order has two buckets (10s + 10min)
    assert len(snap["post_order"]) == 2
