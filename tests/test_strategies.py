"""Tests for individual strategies — focused on the v2/v3/v4 patches."""

from __future__ import annotations

import asyncio

import pytest

from polybot.strategies.maker_edge import MakerEdgeStrategy
from polybot.strategies.overshoot_reversion import OvershootReversionStrategy
from polybot.strategies.boundary_decay import BoundaryDecayStrategy


# ─────────────────────────────────────────────────────────────────────────────
#  MakerEdge inventory tests — the round-trip-not-double-counted bug
# ─────────────────────────────────────────────────────────────────────────────


class TestMakerEdgeInventory:
    def test_inventory_starts_empty(self):
        s = MakerEdgeStrategy()
        assert s._inventory_shares.get("m-test", 0.0) == 0.0
        assert s._current_notional_usd("m-test", 0.5) == 0.0

    def test_buy_increases_inventory(self):
        s = MakerEdgeStrategy()
        s.update_inventory("m-test", delta_shares=10.0, fill_price=0.50)
        assert s._inventory_shares["m-test"] == 10.0
        # Notional from |10| × 0.5 = 5.0
        assert abs(s._current_notional_usd("m-test", 0.50) - 5.0) < 1e-6

    def test_sell_decreases_inventory(self):
        s = MakerEdgeStrategy()
        s.update_inventory("m-test", delta_shares=10.0, fill_price=0.50)
        s.update_inventory("m-test", delta_shares=-4.0, fill_price=0.52)
        assert abs(s._inventory_shares["m-test"] - 6.0) < 1e-6
        # Notional uses current mid (passed as arg)
        assert abs(s._current_notional_usd("m-test", 0.52) - 6.0 * 0.52) < 1e-6

    def test_round_trip_zero_notional(self):
        """The bug fix: BUY+SELL of equal size must zero out notional."""
        s = MakerEdgeStrategy()
        s.update_inventory("m-test", delta_shares=10.0, fill_price=0.50)
        s.update_inventory("m-test", delta_shares=-10.0, fill_price=0.51)
        assert s._inventory_shares["m-test"] == 0.0
        assert s._current_notional_usd("m-test", 0.50) == 0.0
        assert s._current_notional_usd("m-test", 1.00) == 0.0

    def test_short_inventory_notional_is_absolute(self):
        s = MakerEdgeStrategy()
        s.update_inventory("m-test", delta_shares=-10.0, fill_price=0.50)
        assert s._inventory_shares["m-test"] == -10.0
        # |-10| × 0.5 = 5.0 (notional is always >= 0)
        assert abs(s._current_notional_usd("m-test", 0.50) - 5.0) < 1e-6

    def test_reset_inventory(self):
        s = MakerEdgeStrategy()
        s.update_inventory("m-test", delta_shares=10.0, fill_price=0.50)
        s.reset_inventory("m-test")
        assert "m-test" not in s._inventory_shares
        assert s._current_notional_usd("m-test", 0.50) == 0.0

    def test_multiple_markets_independent(self):
        s = MakerEdgeStrategy()
        s.update_inventory("m-A", delta_shares=10.0, fill_price=0.50)
        s.update_inventory("m-B", delta_shares=20.0, fill_price=0.40)
        assert s._inventory_shares["m-A"] == 10.0
        assert s._inventory_shares["m-B"] == 20.0
        assert abs(s._current_notional_usd("m-A", 0.50) - 5.0) < 1e-6
        assert abs(s._current_notional_usd("m-B", 0.40) - 8.0) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
#  Overshoot — sigma threshold patched 30→10
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestOvershootReversion:
    async def test_returns_none_when_feed_cold(self, make_snapshot):
        s = OvershootReversionStrategy()
        # No exchange feed wired
        snap = make_snapshot(end_offset_s=120.0)
        result = await s.evaluate(snap)
        assert result is None

    async def test_returns_none_when_no_burst(self, make_snapshot, warm_feed):
        s = OvershootReversionStrategy()
        s.set_exchange_feed(warm_feed)
        # poly_move_5s = 0 → no burst → block
        snap = make_snapshot(end_offset_s=120.0, mid=0.5, poly_move_5s=0.0)
        result = await s.evaluate(snap)
        assert result is None

    async def test_sigma_buckets_threshold_lowered(self, warm_feed):
        """Confirm the patched value of _MIN_SIGMA_BUCKETS = 10."""
        from polybot.strategies.overshoot_reversion import _MIN_SIGMA_BUCKETS
        assert _MIN_SIGMA_BUCKETS == 10


# ─────────────────────────────────────────────────────────────────────────────
#  Boundary decay — hard time floor at 25s
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestBoundaryDecay:
    async def test_returns_none_below_hard_floor(self, make_snapshot, warm_feed):
        """Below 25s, must return None even if other gates would pass."""
        s = BoundaryDecayStrategy()
        s.set_exchange_feed(warm_feed)
        snap = make_snapshot(end_offset_s=20.0, mid=0.95)
        result = await s.evaluate(snap)
        assert result is None

    async def test_returns_none_above_max_time(self, make_snapshot, warm_feed):
        """Above max_time_remaining=60s, also blocked."""
        s = BoundaryDecayStrategy()
        s.set_exchange_feed(warm_feed)
        snap = make_snapshot(end_offset_s=120.0, mid=0.95)
        result = await s.evaluate(snap)
        assert result is None

    async def test_hard_floor_constant(self):
        from polybot.strategies.boundary_decay import _HARD_TIME_FLOOR_S
        assert _HARD_TIME_FLOOR_S == 25.0
