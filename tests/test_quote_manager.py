"""Tests for QuoteManager — paper mode (live mode tested in integration)."""

from __future__ import annotations

import pytest

from polybot.execution.quote_manager import QuoteManager, QuoteManagerConfig
from polybot.strategies.maker_quoting import MakerQuotingConfig, MakerQuotingStrategy


@pytest.fixture
def qm():
    strat = MakerQuotingStrategy(MakerQuotingConfig(min_half_spread_cents=2.0))
    return QuoteManager(
        market_id="m-1",
        yes_token_id="tok-yes",
        no_token_id="tok-no",
        strategy=strat,
        client=None,
        config=QuoteManagerConfig(
            stale_threshold_cents=1.5,
            max_quote_lifetime_s=30,
            flatten_before_expiry_s=10,
            target_size_shares=5,
        ),
        is_paper=True,
    )


@pytest.mark.asyncio
async def test_first_sync_places_both_sides(qm):
    result = await qm.sync_quotes(
        fair_value=0.50,
        fee_rate=0.072,
        vol_annual=0.45,
        time_remaining_s=180,
    )
    assert result["action"] == "synced"
    assert result["placed"] == 2
    assert len(qm.state.resting) == 2


@pytest.mark.asyncio
async def test_idempotent_sync_does_not_replace(qm):
    await qm.sync_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=180,
    )
    first_ids = set(qm.state.resting.keys())
    result = await qm.sync_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=180,
    )
    assert result["action"] == "synced"
    # No new orders should have been placed
    assert result["placed"] == 0
    assert set(qm.state.resting.keys()) == first_ids


@pytest.mark.asyncio
async def test_drift_replaces_stale_quotes(qm):
    await qm.sync_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=180,
    )
    # 3¢ drift exceeds the 1.5¢ threshold → cancel + replace
    result = await qm.sync_quotes(
        fair_value=0.53, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=180,
    )
    assert result["cancelled"] >= 1
    assert result["placed"] >= 1


@pytest.mark.asyncio
async def test_flatten_window_short_circuits(qm):
    await qm.sync_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=180,
    )
    assert len(qm.state.resting) == 2
    result = await qm.sync_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=5,
    )
    assert result["action"] == "flatten_window"
    assert len(qm.state.resting) == 0


@pytest.mark.asyncio
async def test_cancel_all_clears_state(qm):
    await qm.sync_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=180,
    )
    assert len(qm.state.resting) == 2
    n = await qm.cancel_all()
    assert n == 2
    assert qm.state.resting == {}


@pytest.mark.asyncio
async def test_on_feed_disconnect_cancels(qm):
    await qm.sync_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=180,
    )
    n = await qm.on_feed_disconnect(reason="binance_stale")
    assert n == 2


@pytest.mark.asyncio
async def test_on_fill_reduces_resting_size(qm):
    await qm.sync_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=180,
    )
    oid = next(iter(qm.state.resting.keys()))
    original_size = qm.state.resting[oid].size
    qm.on_fill(order_id=oid, filled_shares=original_size / 2)
    assert qm.state.resting[oid].size == pytest.approx(original_size / 2)
    qm.on_fill(order_id=oid, filled_shares=original_size)
    assert oid not in qm.state.resting
