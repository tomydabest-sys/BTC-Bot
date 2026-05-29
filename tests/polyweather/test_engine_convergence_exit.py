"""Convergence-exit: sell open positions into a favorable CLOB move.

Brief §5.2.3. In live-data mode the engine reprices each open position to the
real CLOB bid (the price we could actually sell our outcome token into) and,
when that bid has risen >= convergence_exit_threshold above entry, realises the
gain early instead of holding to binary resolution.

Pinned here:
  * a favorable move triggers an early exit at the real bid (P&L = bid-entry),
    independent of the pre-sampled binary outcome;
  * an unfavorable / small move does NOT exit but still refreshes the mark to
    the real bid (honest MTM — never the pre-sampled outcome);
  * Brier stays a forecast measure: the realised TradePair carries the
    pre-sampled outcome, not the early-exit price.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from polybot.polyweather.exchanges.clob_client import MockCLOBClient
from polybot.polyweather.orchestrator.engine import OpenPaperPosition


def _position(*, entry: str, final_outcome: int, token_id: str = "tok-1") -> OpenPaperPosition:
    now = time.time()
    return OpenPaperPosition(
        market_id="mkt-1", event_id="ev-1", strategy="weather_ensemble",
        station="KMIA", city="Miami", side="BUY", outcome="Yes", token_id=token_id,
        entry_price=Decimal(entry), current_price=Decimal(entry),
        size_tokens=Decimal("100"), size_usdc=Decimal("20"),
        p_model=0.25, p_realised=0.25, bucket_low=84.0, bucket_high=85.0,
        horizon_hours=8.0, opened_at=now - 60, closes_at=now + 3600,
        rebate_usdc=Decimal("0.01"), final_outcome=final_outcome,
        fill_latency_seconds=1.0,
    )


@pytest.mark.asyncio
async def test_favorable_move_triggers_convergence_exit(engine_factory):
    engine, store = engine_factory()
    engine.config.convergence_exit_threshold = 0.10
    # Real book midpoint 0.95 → best bid 0.93 (mock book sits 0.02 below mid).
    engine.clob = MockCLOBClient(default_mid=0.95)

    # Sampled to LOSE (final_outcome=0): the early exit must still realise the
    # real interim gain — selling into the move doesn't depend on the outcome.
    pos = _position(entry="0.20", final_outcome=0)
    engine.risk.record_open(pos.size_usdc, strategy=pos.strategy)
    engine._open_positions = [pos]

    await engine._reprice_open_positions("00001")

    assert engine._open_positions == [], "position should have been sold into the move"
    assert engine.metrics.convergence_exits == 1
    trades = store.trades(limit=10)
    assert len(trades) == 1
    t = trades[0]
    assert t.exit_price == Decimal("0.9300")
    # (0.93 - 0.20) * 100 + 0.01 rebate
    assert t.realised_pnl_usdc == Decimal("73.0100")
    # Brier integrity: the recorded outcome is the pre-sampled binary outcome,
    # not the early-exit price.
    assert t.realised_outcome == 0
    assert t.metadata.get("exit_reason") == "convergence_exit"


@pytest.mark.asyncio
async def test_small_move_holds_but_refreshes_mark(engine_factory):
    engine, store = engine_factory()
    engine.config.convergence_exit_threshold = 0.10
    # Midpoint 0.21 → best bid 0.19, below the 0.20 entry: no exit.
    engine.clob = MockCLOBClient(default_mid=0.21)

    pos = _position(entry="0.20", final_outcome=1)
    engine.risk.record_open(pos.size_usdc, strategy=pos.strategy)
    engine._open_positions = [pos]

    await engine._reprice_open_positions("00001")

    assert len(engine._open_positions) == 1, "position should still be open"
    assert engine.metrics.convergence_exits == 0
    assert store.trades(limit=10) == []
    # Honest MTM: marked to the real bid, not the entry or the known outcome.
    assert engine._open_positions[0].current_price == Decimal("0.1900")


@pytest.mark.asyncio
async def test_disabled_flag_holds_through_favorable_move(engine_factory):
    engine, store = engine_factory()
    engine.config.convergence_exit_enabled = False
    engine.config.convergence_exit_threshold = 0.10
    engine.clob = MockCLOBClient(default_mid=0.95)

    pos = _position(entry="0.20", final_outcome=1)
    engine.risk.record_open(pos.size_usdc, strategy=pos.strategy)
    engine._open_positions = [pos]

    await engine._reprice_open_positions("00001")

    # Still marks to the real bid, but does not early-exit when disabled.
    assert len(engine._open_positions) == 1
    assert engine.metrics.convergence_exits == 0
    assert engine._open_positions[0].current_price == Decimal("0.9300")
