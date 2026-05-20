"""Tests for the paper-validation gate."""

from __future__ import annotations

import time

import pytest

from polybot.monitoring.paper_validation import (
    GateThresholds,
    PaperValidationGate,
)


def _make_gate(tmp_path, **overrides) -> PaperValidationGate:
    path = str(tmp_path / "validation.json")
    return PaperValidationGate(
        state_path=path,
        thresholds=overrides.pop("thresholds", None) or GateThresholds(),
        starting_equity_usd=500.0,
        **overrides,
    )


def test_fresh_gate_has_started_at_now(tmp_path):
    gate = _make_gate(tmp_path)
    assert gate.state.started_at > 0
    assert abs(gate.state.started_at - time.time()) < 5


def test_fresh_gate_status_is_insufficient_data(tmp_path):
    gate = _make_gate(tmp_path)
    report = gate.evaluate()
    assert report.status == "INSUFFICIENT_DATA"


def test_round_trip_recording_updates_state(tmp_path):
    gate = _make_gate(tmp_path)
    gate.record_round_trip(gross_pnl_usd=0.50, fees_paid_usd=0.05)
    assert gate.state.trade_count == 1
    assert gate.state.gross_pnl_usd == pytest.approx(0.50)
    assert gate.state.fees_paid_usd == pytest.approx(0.05)
    assert gate.state.net_pnl_usd == pytest.approx(0.45)
    assert gate.state.equity_curve[-1] == pytest.approx(500.45)


def test_dynamic_fee_consistency_tracked(tmp_path):
    gate = _make_gate(tmp_path)
    for _ in range(10):
        gate.record_round_trip(
            gross_pnl_usd=0.10, fees_paid_usd=0.02, dynamic_fee_fetched=True
        )
    assert gate.state.fee_consistency_pct == 100.0
    # One hardcoded order pair contaminates the rate.
    gate.record_round_trip(
        gross_pnl_usd=0.10, fees_paid_usd=0.02, dynamic_fee_fetched=False
    )
    assert gate.state.fee_consistency_pct < 100.0


def test_drawdown_computed_from_curve(tmp_path):
    gate = _make_gate(tmp_path)
    # Push equity up then down to engineer a drawdown
    for pnl in [10, 10, 10, -25]:  # +30 then -25 = -25 from peak 530
        gate.record_round_trip(gross_pnl_usd=pnl, fees_paid_usd=0)
    dd = gate.state.max_drawdown_pct
    # Peak = 530; trough = 505; dd = 25/530 = ~4.7%
    assert dd == pytest.approx(25 / 530, abs=1e-4)


def test_sharpe_zero_when_no_returns(tmp_path):
    gate = _make_gate(tmp_path)
    assert gate.state.sharpe_daily == 0.0


def test_sharpe_computed_from_daily_returns(tmp_path):
    gate = _make_gate(tmp_path)
    for r in [0.01, 0.012, 0.008, 0.011, 0.009]:
        gate.record_daily_return(r)
    sharpe = gate.state.sharpe_daily
    assert sharpe > 0
    # mean ~0.01, stdev ~0.0016 → sharpe ~6 (extremely high — these are
    # very stable returns)
    assert sharpe > 1.0


def test_evaluate_not_ready_with_failing_metrics(tmp_path):
    gate = _make_gate(tmp_path)
    # Record a single trade so we leave INSUFFICIENT_DATA
    gate.record_round_trip(gross_pnl_usd=1.0, fees_paid_usd=0.1)
    report = gate.evaluate()
    assert report.status == "NOT_READY"
    assert "duration_days" in report.failed
    assert "trades" in report.failed


def test_evaluate_ready_when_all_pass(tmp_path):
    thresholds = GateThresholds(
        min_duration_days=0.0,
        min_trades=1,
        min_net_pnl_usd=0.0,
        min_sharpe_daily=0.0,
        max_drawdown_pct=1.0,
        min_quote_uptime_pct=0.0,
        max_p95_latency_ms=10_000.0,
        max_unhandled_exceptions=0,
        min_fee_consistency_pct=0.0,
    )
    gate = _make_gate(tmp_path, thresholds=thresholds)
    gate.record_round_trip(gross_pnl_usd=1.0, fees_paid_usd=0.05)
    gate.record_daily_return(0.01)
    gate.record_p95_latency(50)
    gate.record_quote_uptime(95)
    report = gate.evaluate()
    assert report.status == "READY", f"failed: {report.failed}"


def test_unhandled_exception_blocks_ready_status(tmp_path):
    thresholds = GateThresholds(
        min_duration_days=0.0,
        min_trades=1,
        min_net_pnl_usd=0.0,
        min_sharpe_daily=0.0,
        max_drawdown_pct=1.0,
        min_quote_uptime_pct=0.0,
        max_p95_latency_ms=10_000.0,
        min_fee_consistency_pct=0.0,
    )
    gate = _make_gate(tmp_path, thresholds=thresholds)
    gate.record_round_trip(gross_pnl_usd=1.0)
    gate.record_daily_return(0.01)
    gate.record_unhandled_exception()
    report = gate.evaluate()
    assert report.status == "NOT_READY"
    assert "unhandled_exceptions" in report.failed


def test_state_persists_across_instances(tmp_path):
    path = str(tmp_path / "v.json")
    g1 = PaperValidationGate(state_path=path, starting_equity_usd=500.0)
    g1.record_round_trip(gross_pnl_usd=2.0, fees_paid_usd=0.1)
    g1.save()

    g2 = PaperValidationGate(state_path=path, starting_equity_usd=500.0)
    assert g2.state.trade_count == 1
    assert g2.state.gross_pnl_usd == pytest.approx(2.0)


def test_reset_wipes_everything(tmp_path):
    gate = _make_gate(tmp_path)
    gate.record_round_trip(gross_pnl_usd=5.0, fees_paid_usd=0.1)
    assert gate.state.trade_count == 1
    gate.reset()
    assert gate.state.trade_count == 0
    assert gate.state.gross_pnl_usd == 0


def test_report_dict_serialises_cleanly(tmp_path):
    gate = _make_gate(tmp_path)
    gate.record_round_trip(gross_pnl_usd=0.5)
    report_dict = gate.evaluate().as_dict()
    assert "status" in report_dict
    assert "metrics" in report_dict
    assert isinstance(report_dict["metrics"], list)
    assert all("name" in m for m in report_dict["metrics"])


# ─────────────────────────────────────────────────────────────────────────
# Round-trip P&L tracker — Phase 3 wiring
# ─────────────────────────────────────────────────────────────────────────


def test_round_trip_tracker_yes_buy_then_yes_sell_reports_gross_pnl(tmp_path):
    """YES BUY @ 0.45 followed by YES SELL @ 0.50 on 10 shares must surface
    as a single round-trip with gross_pnl = (0.50 - 0.45) * 10 = $0.50.

    This is the brief's canonical case: closing inventory back through
    zero on the same token via an opposite-side fill.
    """
    from polybot.execution.maker_orchestrator import _RoundTripTracker

    tracker = _RoundTripTracker(fee_theta=0.072)
    opened = tracker.on_fill(side_yes=True, is_buy=True, shares=10, price=0.45)
    assert opened == []  # opening leg, no round-trip yet
    closed = tracker.on_fill(side_yes=True, is_buy=False, shares=10, price=0.50)
    assert len(closed) == 1
    rt = closed[0]
    assert rt.gross_pnl_usd == pytest.approx(0.50, abs=1e-6)
    # Fees should be non-zero (fee_at_price > 0 in the 0.4-0.5 band) but
    # the gate cares about net so we only sanity-check sign.
    assert rt.fees_paid_usd > 0.0
    # Inventory tracker should be flat after the close.
    assert tracker.open_lot_count == 0


def test_round_trip_tracker_cross_token_buy_buy(tmp_path):
    """YES BUY 10 @ 0.45 + NO BUY 10 @ 0.50: net YES inventory back through
    zero via the mirror token. Gross P&L = (1 - 0.45 - 0.50) * 10 = $0.50."""
    from polybot.execution.maker_orchestrator import _RoundTripTracker

    tracker = _RoundTripTracker(fee_theta=0.072)
    tracker.on_fill(side_yes=True, is_buy=True, shares=10, price=0.45)
    closed = tracker.on_fill(side_yes=False, is_buy=True, shares=10, price=0.50)
    assert len(closed) == 1
    assert closed[0].gross_pnl_usd == pytest.approx(0.50, abs=1e-6)
    assert tracker.open_lot_count == 0


def test_round_trip_tracker_partial_close_then_residual(tmp_path):
    """Open 10, close 6 → one round-trip for 6, 4 still open."""
    from polybot.execution.maker_orchestrator import _RoundTripTracker

    tracker = _RoundTripTracker(fee_theta=0.0)  # no fees for clarity
    tracker.on_fill(side_yes=True, is_buy=True, shares=10, price=0.45)
    closed = tracker.on_fill(side_yes=True, is_buy=False, shares=6, price=0.50)
    assert len(closed) == 1
    assert closed[0].gross_pnl_usd == pytest.approx(0.05 * 6, abs=1e-6)
    assert tracker.open_lot_count == 1
    # Net inventory should still be +4 long YES.
    assert tracker.net_signed_inventory == pytest.approx(4.0)


def test_paper_fill_sweep_records_real_pnl_through_validation_gate(tmp_path):
    """End-to-end: feed two opposing fills into a market state via the
    tracker on the orchestrator and verify the gate sees a non-zero P&L."""
    from polybot.execution.maker_orchestrator import _RoundTripTracker

    gate = _make_gate(tmp_path)
    # Simulate the sweep accounting by hand (the sweep glues these calls
    # together; here we test the validation hand-off in isolation).
    state_tracker = _RoundTripTracker(fee_theta=0.0)

    # YES BUY @ 0.45 — opens.
    rts = state_tracker.on_fill(side_yes=True, is_buy=True, shares=10, price=0.45)
    assert rts == []

    # NO BUY @ 0.50 — closes via the mirror token.
    rts = state_tracker.on_fill(side_yes=False, is_buy=True, shares=10, price=0.50)
    assert len(rts) == 1
    for rt in rts:
        gate.record_round_trip(
            gross_pnl_usd=rt.gross_pnl_usd,
            fees_paid_usd=rt.fees_paid_usd,
            dynamic_fee_fetched=True,
        )

    assert gate.state.gross_pnl_usd == pytest.approx(0.50, abs=1e-6)
    assert gate.state.trade_count == 1


def test_seconds_until_next_utc_midnight_is_positive():
    """Smoke check on the daily-returns ticker's scheduling helper."""
    from polybot.execution.maker_orchestrator import MakerOrchestrator

    # Just past midnight UTC → should be ~86400s until the next.
    just_past_midnight = 86400.0 * 12345 + 5.0
    assert MakerOrchestrator._seconds_until_next_utc_midnight(just_past_midnight) == (
        86400.0 - 5.0
    )


# ─────────────────────────────────────────────────────────────────────────
# Realistic paper P&L — naked inventory must book its real loss
# ─────────────────────────────────────────────────────────────────────────


def test_falling_knife_inventory_books_loss_on_close():
    """Buying YES all the way down a crashing mid, then closing at the low,
    must realize a LOSS — not vanish. This is the bug that produced the
    fake +13%/15min paper P&L: naked accumulation was never marked out."""
    from polybot.execution.maker_orchestrator import _RoundTripTracker

    tracker = _RoundTripTracker(fee_theta=0.0)
    # YES bids filled on every downtick as the market crashes toward 0.
    tracker.on_fill(side_yes=True, is_buy=True, shares=10, price=0.40)
    tracker.on_fill(side_yes=True, is_buy=True, shares=10, price=0.20)
    tracker.on_fill(side_yes=True, is_buy=True, shares=10, price=0.05)
    # avg entry = (0.40 + 0.20 + 0.05) / 3 = 0.2167 over 30 shares
    assert tracker.net_signed_inventory == pytest.approx(30.0)
    assert tracker.avg_entry_yes == pytest.approx((0.40 + 0.20 + 0.05) / 3, abs=1e-6)

    # Market resolves "Down" → YES settles near 0. Realize at 0.01.
    rt = tracker.realize_remaining(exit_price_yes=0.01)
    assert rt is not None
    expected = (0.01 - (0.40 + 0.20 + 0.05) / 3) * 30  # strongly negative
    assert rt.gross_pnl_usd == pytest.approx(expected, abs=1e-6)
    assert rt.gross_pnl_usd < -6.0, "falling-knife close must be a real loss"
    # Position is flat afterwards.
    assert tracker.open_lot_count == 0
    assert tracker.realize_remaining(exit_price_yes=0.01) is None


def test_realize_remaining_none_when_flat():
    from polybot.execution.maker_orchestrator import _RoundTripTracker

    tracker = _RoundTripTracker()
    assert tracker.realize_remaining(exit_price_yes=0.5) is None


@pytest.mark.asyncio
async def test_orchestrator_books_loss_when_market_rolls_off(tmp_path):
    """Integration: a market that accumulates long YES and then rolls off
    the scanner must realize the position's loss onto the validation gate
    (previously the state was dropped and the P&L disappeared)."""
    from datetime import UTC, datetime, timedelta

    from polybot.config import MakerConfig
    from polybot.data.exchange_feed import PriceFeedState
    from polybot.data.models import Market
    from polybot.execution.maker_orchestrator import (
        MakerOrchestrator,
        _MarketState,
    )
    from polybot.execution.quote_manager import QuoteManager
    from polybot.monitoring.paper_validation import PaperValidationGate
    from polybot.positions.inventory import InventoryManager
    from polybot.strategies.maker_quoting import (
        MakerQuotingConfig,
        MakerQuotingStrategy,
    )

    class _Scanner:
        def __init__(self):
            self.active_markets = {}

    class _Feed:
        def __init__(self):
            self._s = PriceFeedState()
            self._s.push(95000.0)

        @property
        def last_price(self):
            return self._s.last_price

        @property
        def feed_age_s(self):
            return 0.0

    gate = PaperValidationGate(
        state_path=str(tmp_path / "v.json"), starting_equity_usd=500.0
    )
    cfg = MakerConfig(enabled=True, primary_markets=["btc-5m"])
    orch = MakerOrchestrator(
        maker_cfg=cfg,
        scanner=_Scanner(),
        pipeline=object(),
        exchange_feed=_Feed(),
        is_paper=True,
        client=None,
        telegram=None,
        validation_gate=gate,
    )

    market = Market(
        id="m-roll",
        question="Bitcoin Up or Down?",
        slug="btc-updown-5m-1",
        outcomes=["Up", "Down"],
        token_ids=["y", "n"],
        end_date=datetime.now(UTC) + timedelta(minutes=4),
        category="crypto",
        active=True,
    )
    qm = QuoteManager(
        market_id=market.id, yes_token_id="y", no_token_id="n",
        strategy=MakerQuotingStrategy(MakerQuotingConfig()),
        client=None, is_paper=True,
    )
    state = _MarketState(
        market=market, quote_manager=qm,
        inventory=InventoryManager(market_id=market.id),
        last_mid=0.02,  # crashed
    )
    # Accumulate long YES bought high.
    state.round_trip_tracker.on_fill(side_yes=True, is_buy=True, shares=30, price=0.40)
    orch._states[market.id] = state

    # Scanner now has no eligible markets → state rolls off and must book loss.
    await orch._maintain_states()

    assert market.id not in orch._states
    assert gate.state.trade_count == 1
    # Bought 30 @ 0.40, realized at 0.02 → ~ -$11.4 gross.
    assert gate.state.gross_pnl_usd < -10.0
