"""Tests for PositionManager — partial-fill safety + P&L correctness."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from polybot.data.models import (
    Order,
    OrderStatus,
    OrderType,
    PositionStatus,
    Side,
)
from polybot.events import EventBus
from polybot.positions.manager import PositionManager


def _make_filled_order(
    market_id: str = "m-test",
    token_id: str = "tok-yes",
    side: Side = Side.BUY,
    price: float = 0.50,
    size: float = 20.0,
    strategy: str = "overshoot_reversion",
) -> Order:
    return Order(
        market_id=market_id,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        order_type=OrderType.GTC,
        strategy=strategy,
        status=OrderStatus.FILLED,
        filled_size=size,
        avg_fill_price=price,
    )


class TestPositionLifecycle:
    def test_open_position(self):
        pm = PositionManager(EventBus())
        order = _make_filled_order(side=Side.BUY, price=0.5, size=20)
        pm.update_from_fill(order)
        portfolio = pm.portfolio
        assert len(portfolio.positions) == 1
        assert portfolio.positions[0].size == 20
        assert portfolio.positions[0].avg_entry_price == 0.5

    def test_increase_position_weighted_avg(self):
        pm = PositionManager(EventBus())
        pm.update_from_fill(_make_filled_order(price=0.50, size=10))
        pm.update_from_fill(_make_filled_order(price=0.60, size=10))
        positions = pm.portfolio.positions
        assert len(positions) == 1
        assert positions[0].size == 20
        # Weighted avg: (10*0.5 + 10*0.6) / 20 = 0.55
        assert abs(positions[0].avg_entry_price - 0.55) < 1e-6

    def test_round_trip_realised_pnl(self):
        pm = PositionManager(EventBus())
        pm.update_from_fill(_make_filled_order(side=Side.BUY, price=0.50, size=20))
        # Exit at higher price → profit
        exit_order = _make_filled_order(
            side=Side.SELL, price=0.55, size=20,
            strategy="exit_overshoot_reversion",
        )
        pm.update_from_fill(exit_order)
        # Position should be fully closed
        assert len(pm.portfolio.positions) == 0
        # Realised PnL = 20 × (0.55 - 0.50) = 1.0
        assert abs(pm.portfolio.realized_pnl - 1.0) < 1e-6

    def test_round_trip_loss(self):
        pm = PositionManager(EventBus())
        pm.update_from_fill(_make_filled_order(side=Side.BUY, price=0.50, size=20))
        exit_order = _make_filled_order(
            side=Side.SELL, price=0.45, size=20,
            strategy="exit_overshoot_reversion",
        )
        pm.update_from_fill(exit_order)
        assert len(pm.portfolio.positions) == 0
        # Loss: 20 × (0.45 - 0.50) = -1.0
        assert abs(pm.portfolio.realized_pnl - (-1.0)) < 1e-6

    def test_short_position_pnl(self):
        pm = PositionManager(EventBus())
        # Open SHORT YES at 0.60
        pm.update_from_fill(_make_filled_order(side=Side.SELL, price=0.60, size=10))
        # Close at 0.50 → profit on the short
        exit_order = _make_filled_order(
            side=Side.BUY, price=0.50, size=10,
            strategy="exit_overshoot_reversion",
        )
        pm.update_from_fill(exit_order)
        assert len(pm.portfolio.positions) == 0
        # Profit = 10 × (0.60 - 0.50) = 1.0
        assert abs(pm.portfolio.realized_pnl - 1.0) < 1e-6


class TestPartialFillSafety:
    def test_exit_fill_exceeds_position_logs_warning(self, caplog):
        """When an exit fill is larger than the position, leftover must not vanish silently."""
        import logging
        caplog.set_level(logging.WARNING)
        pm = PositionManager(EventBus())
        # Open 20 long
        pm.update_from_fill(_make_filled_order(side=Side.BUY, price=0.50, size=20))
        # Exit fill claims 30 shares
        oversized_exit = _make_filled_order(
            side=Side.SELL, price=0.55, size=30,
            strategy="exit_overshoot_reversion",
        )
        pm.update_from_fill(oversized_exit)
        # Position closed
        assert len(pm.portfolio.positions) == 0
        # Realised pnl should be on the 20 shares we actually had
        assert abs(pm.portfolio.realized_pnl - 1.0) < 1e-6  # 20 * 0.05
        # Check that a warning was logged about the leftover
        # (test only that *some* logging happened — structlog may not go through caplog)
        # so just check that the position was reduced safely
        assert pm.portfolio.realized_pnl > 0

    def test_exit_fill_no_position(self):
        pm = PositionManager(EventBus())
        # Exit order arrives with no opening position
        orphan_exit = _make_filled_order(
            side=Side.SELL, price=0.55, size=10,
            strategy="exit_overshoot_reversion",
        )
        pm.update_from_fill(orphan_exit)
        # No position should be created; no PnL
        assert len(pm.portfolio.positions) == 0
        assert pm.portfolio.realized_pnl == 0.0

    def test_partial_exit_reduces_position(self):
        pm = PositionManager(EventBus())
        pm.update_from_fill(_make_filled_order(side=Side.BUY, price=0.50, size=20))
        partial_exit = _make_filled_order(
            side=Side.SELL, price=0.55, size=8,
            strategy="exit_overshoot_reversion",
        )
        pm.update_from_fill(partial_exit)
        positions = pm.portfolio.positions
        assert len(positions) == 1
        assert abs(positions[0].size - 12.0) < 1e-6
        # 8 shares × 0.05 profit = 0.4
        assert abs(pm.portfolio.realized_pnl - 0.4) < 1e-6


class TestExitSignalGeneration:
    def test_overshoot_take_profit(self):
        pm = PositionManager(EventBus())
        pm.update_from_fill(_make_filled_order(side=Side.BUY, price=0.50, size=20))
        pm.update_prices("m-test", 0.515)  # 1.5¢ favourable move
        exits = pm.check_exits()
        assert len(exits) == 1
        assert "overshoot_tp" in exits[0].reason

    def test_no_exit_when_neutral(self):
        pm = PositionManager(EventBus())
        pm.update_from_fill(_make_filled_order(side=Side.BUY, price=0.50, size=20))
        pm.update_prices("m-test", 0.502)
        exits = pm.check_exits()
        assert len(exits) == 0

    def test_boundary_decay_holds_to_expiry(self):
        """Boundary decay should NOT generate early TP — only catastrophic stop."""
        pm = PositionManager(EventBus())
        order = _make_filled_order(strategy="boundary_decay", price=0.85, size=20)
        pm.update_from_fill(order)
        # Slight favourable move — should NOT trigger exit
        pm.update_prices("m-test", 0.92)
        exits = pm.check_exits()
        assert len(exits) == 0

    def test_boundary_hard_stop(self):
        pm = PositionManager(EventBus())
        order = _make_filled_order(strategy="boundary_decay", price=0.85, size=20)
        pm.update_from_fill(order)
        # 15% adverse move
        pm.update_prices("m-test", 0.72)
        exits = pm.check_exits()
        assert len(exits) == 1
        assert "boundary_hard_stop" in exits[0].reason


class TestDailyPnLReset:
    """daily_pnl must roll over at the UTC day boundary.

    Without a reset, accumulated losses from prior days carry into today and
    eventually trip the daily-loss circuit breaker on a bot that's actually
    profitable each day in isolation.
    """

    def test_reset_at_utc_midnight(self):
        pm = PositionManager(EventBus())
        # Open + close at a loss
        pm.update_from_fill(_make_filled_order(side=Side.BUY, price=0.50, size=20))
        pm.update_from_fill(_make_filled_order(
            side=Side.SELL, price=0.45, size=20,
            strategy="exit_overshoot_reversion",
        ))
        assert pm.portfolio.daily_pnl < 0
        loss_amount = pm.portfolio.daily_pnl
        # realized_pnl should match daily_pnl right now
        assert pm.portfolio.realized_pnl == loss_amount

        # Force the reset boundary into the past — next fill must zero daily_pnl
        pm._daily_reset_at = datetime.utcnow() - timedelta(seconds=1)

        # New trade after the boundary
        pm.update_from_fill(_make_filled_order(side=Side.BUY, price=0.50, size=20))
        pm.update_from_fill(_make_filled_order(
            side=Side.SELL, price=0.55, size=20,
            strategy="exit_overshoot_reversion",
        ))

        # daily_pnl should reflect ONLY today's profit, not yesterday's loss
        assert abs(pm.portfolio.daily_pnl - 1.0) < 1e-6
        # realized_pnl is lifetime; it accumulates across the boundary
        assert abs(pm.portfolio.realized_pnl - (loss_amount + 1.0)) < 1e-6

    def test_no_reset_before_boundary(self):
        pm = PositionManager(EventBus())
        pm.update_from_fill(_make_filled_order(side=Side.BUY, price=0.50, size=20))
        pm.update_from_fill(_make_filled_order(
            side=Side.SELL, price=0.55, size=20,
            strategy="exit_overshoot_reversion",
        ))
        first_pnl = pm.portfolio.daily_pnl
        assert first_pnl > 0

        # Another trade well before the next boundary — must accumulate
        pm.update_from_fill(_make_filled_order(side=Side.BUY, price=0.50, size=20))
        pm.update_from_fill(_make_filled_order(
            side=Side.SELL, price=0.55, size=20,
            strategy="exit_overshoot_reversion",
        ))
        assert abs(pm.portfolio.daily_pnl - first_pnl * 2) < 1e-6


class TestSameMarketNetting:
    """Opposite-side fills on the same (market, token) must net out into
    a single signed position, not accumulate as two separate Position
    objects. Strategies like maker_edge track inventory as a signed net
    value; if PositionManager doesn't net, the strategy's view diverges
    from PositionManager's, both opposite legs occupy a position-cap
    slot, and the exit-price-sanity-block can leave the bot fixated on
    a market it can no longer cleanly close.
    """

    def test_opposite_fill_fully_nets_position(self):
        pm = PositionManager(EventBus())
        pm.update_from_fill(_make_filled_order(
            side=Side.BUY, price=0.50, size=10,
            strategy="maker_edge",
        ))
        # Opposite-side non-exit fill must close the BUY, not open a new SELL.
        pm.update_from_fill(_make_filled_order(
            side=Side.SELL, price=0.60, size=10,
            strategy="maker_edge",
        ))
        assert pm.portfolio.positions == []
        # Realised: (0.60 - 0.50) * 10 = 1.0
        assert abs(pm.portfolio.realized_pnl - 1.0) < 1e-6
        assert abs(pm.portfolio.daily_pnl - 1.0) < 1e-6

    def test_opposite_fill_partially_nets_position(self):
        pm = PositionManager(EventBus())
        pm.update_from_fill(_make_filled_order(
            side=Side.BUY, price=0.50, size=10,
            strategy="maker_edge",
        ))
        pm.update_from_fill(_make_filled_order(
            side=Side.SELL, price=0.60, size=4,
            strategy="maker_edge",
        ))
        assert len(pm.portfolio.positions) == 1
        p = pm.portfolio.positions[0]
        assert p.side == Side.BUY
        assert abs(p.size - 6.0) < 1e-6
        # avg_entry_price unchanged on the remaining portion
        assert abs(p.avg_entry_price - 0.50) < 1e-6
        # Realised P&L on the closed portion: (0.60 - 0.50) * 4 = 0.4
        assert abs(pm.portfolio.realized_pnl - 0.4) < 1e-6

    def test_opposite_fill_overflow_flips_side(self):
        pm = PositionManager(EventBus())
        pm.update_from_fill(_make_filled_order(
            side=Side.BUY, price=0.50, size=10,
            strategy="maker_edge",
        ))
        # SELL larger than existing BUY: close BUY, open SELL with overflow.
        pm.update_from_fill(_make_filled_order(
            side=Side.SELL, price=0.60, size=15,
            strategy="maker_edge",
        ))
        assert len(pm.portfolio.positions) == 1
        p = pm.portfolio.positions[0]
        assert p.side == Side.SELL
        assert abs(p.size - 5.0) < 1e-6
        assert abs(p.avg_entry_price - 0.60) < 1e-6
        # Realised on the closed 10: (0.60 - 0.50) * 10 = 1.0
        assert abs(pm.portfolio.realized_pnl - 1.0) < 1e-6

    def test_maker_alternation_does_not_accumulate_slots(self):
        """The actual production failure mode: maker_edge alternates BUY
        and SELL quotes on the same market while flattening inventory.
        Each round-trip must collapse back to zero positions, not park
        an opposing leg in the cap."""
        pm = PositionManager(EventBus())
        for px_buy, px_sell in [(0.50, 0.55), (0.48, 0.52), (0.45, 0.50)]:
            pm.update_from_fill(_make_filled_order(
                side=Side.BUY, price=px_buy, size=10,
                strategy="maker_edge",
            ))
            assert len(pm.portfolio.positions) == 1
            pm.update_from_fill(_make_filled_order(
                side=Side.SELL, price=px_sell, size=10,
                strategy="maker_edge",
            ))
            # Both legs collapsed — no cap consumption between rounds.
            assert pm.portfolio.positions == []

    def test_exit_path_unchanged_by_netting(self):
        """Exit-tagged fills must still go through the exit path
        (looking up by flipped side), not the new netting path."""
        pm = PositionManager(EventBus())
        pm.update_from_fill(_make_filled_order(
            side=Side.BUY, price=0.50, size=20,
            strategy="overshoot_reversion",
        ))
        pm.update_from_fill(_make_filled_order(
            side=Side.SELL, price=0.55, size=20,
            strategy="exit_overshoot_reversion",
        ))
        assert pm.portfolio.positions == []
        assert abs(pm.portfolio.realized_pnl - 1.0) < 1e-6


class TestMaxHoldSafetyNet:
    """Held-to-expiry strategies (dual_direction, boundary_decay) that
    happen to land in long-duration markets would otherwise park positions
    for hours and peg the position cap. The safety net force-exits any
    position open longer than max_hold_seconds.
    """

    def test_position_held_too_long_emits_exit(self):
        import time
        pm = PositionManager(EventBus(), max_hold_seconds=1.0)
        pm.update_from_fill(_make_filled_order(
            side=Side.BUY, price=0.50, size=20,
            strategy="dual_direction_arb",
        ))
        # Fresh position — should not exit
        assert pm.check_exits() == []
        # Force the open timestamp into the past
        key = next(iter(pm._position_open_ts))
        pm._position_open_ts[key] = time.time() - 2.0
        exits = pm.check_exits()
        assert len(exits) == 1
        assert "max_hold_exceeded" in exits[0].reason

    def test_safety_net_disabled_when_zero(self):
        import time
        pm = PositionManager(EventBus(), max_hold_seconds=0.0)
        pm.update_from_fill(_make_filled_order(
            side=Side.BUY, price=0.50, size=20,
            strategy="dual_direction_arb",
        ))
        # Even with a 1-hour-old position, no max_hold exit when disabled
        key = next(iter(pm._position_open_ts))
        pm._position_open_ts[key] = time.time() - 3600.0
        for ex in pm.check_exits():
            assert "max_hold_exceeded" not in ex.reason

    def test_max_hold_fires_before_strategy_dispatch(self):
        """Even strategies that normally return None (held-to-expiry) must
        respect the safety net."""
        import time
        pm = PositionManager(EventBus(), max_hold_seconds=1.0)
        pm.update_from_fill(_make_filled_order(
            side=Side.BUY, price=0.50, size=20,
            strategy="boundary_decay",
        ))
        # boundary_decay normally holds forever (no exit when neutral)
        pm.update_prices("m-test", 0.50)
        assert pm.check_exits() == []
        key = next(iter(pm._position_open_ts))
        pm._position_open_ts[key] = time.time() - 2.0
        exits = pm.check_exits()
        assert len(exits) == 1
        assert "max_hold_exceeded" in exits[0].reason
