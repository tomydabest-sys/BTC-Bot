"""Regression: unrealized mark-to-market must not balloon the equity curve.

The settlement loop used to walk each open position's price deterministically
toward its pre-sampled binary outcome (0 or 1). Cheap long-shot positions
"destined to win" marked toward $1.00, so a book of open positions inflated
unrealized P&L by +100% of bankroll and then cratered at settlement — the
spike-then-crash the operator saw on the dashboard.

The honest model marks an open binary position at its entry price plus a small
mean-zero wiggle, because the outcome is unknown until it resolves. These tests
pin that: per-position marks stay within ``mtm_noise_pct`` of entry, and total
unrealized stays bounded by ``mtm_noise_pct`` of open exposure.
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_open_position_marks_stay_near_entry(engine_factory):
    # Long hold + several signals per cycle so positions pile up open.
    engine, _store = engine_factory(
        duration=2.0, cycle=0.05, position_horizon=30.0,
        bucket_cooldown=0.2, max_signals_per_cycle=5,
    )
    await engine.start()
    assert len(engine.open_positions) >= 5, "test needs several open positions"

    noise = engine.config.mtm_noise_pct
    for p in engine.open_positions:
        drift = abs(float(p.current_price) - float(p.entry_price)) / float(p.entry_price)
        assert drift <= noise + 1e-6, (
            f"{p.market_id} marked {p.current_price} vs entry {p.entry_price} "
            f"(drift {drift:.4f} > {noise})"
        )


@pytest.mark.asyncio
async def test_unrealized_pnl_is_bounded_by_noise_times_exposure(engine_factory):
    engine, store = engine_factory(
        duration=2.0, cycle=0.05, position_horizon=30.0,
        bucket_cooldown=0.2, max_signals_per_cycle=5,
    )
    await engine.start()
    assert engine.open_positions, "no open positions to mark"

    # |unrealized| <= mtm_noise_pct * sum(entry * tokens) by construction.
    bound = engine.config.mtm_noise_pct * sum(
        float(p.entry_price) * float(p.size_tokens) for p in engine.open_positions
    )
    unreal = abs(float(engine.unrealized_pnl_usdc))
    assert unreal <= bound + 0.05, f"unrealized {unreal} exceeded bound {bound}"

    # Consequently the recorded equity curve never spikes far above bankroll:
    # realized cash plus only a sliver of bounded unrealized.
    eq = [float(b) for _, b in store.equity_history(limit=100000)]
    start = eq[0]
    # Allow generous headroom for realized settlements; the old bug pushed this
    # 20-100% above start.
    assert max(eq) <= start * 1.10, f"equity spiked to {max(eq):.2f} from {start:.2f}"
