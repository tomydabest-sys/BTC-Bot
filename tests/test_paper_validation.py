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
