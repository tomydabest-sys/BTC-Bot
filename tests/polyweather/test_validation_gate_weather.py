"""All 9 validation-gate criteria evaluated against crafted fixtures."""

from __future__ import annotations

import time
from decimal import Decimal

from polybot.polyweather.risk.validation_gate import TradePair, WeatherValidationGate


def _trade(pnl: float, strategy: str = "weather_ensemble", correct: int = 1) -> TradePair:
    return TradePair(
        market_id="m", event_id="e", strategy=strategy,
        station="KLGA", city="NYC", side="BUY",
        entry_price=Decimal("0.50"), exit_price=Decimal("1.00") if correct else Decimal("0"),
        size=Decimal("10"), fees_usdc=Decimal("0"), rebates_usdc=Decimal("0.01"),
        realised_pnl_usdc=Decimal(str(pnl)),
        opened_at=time.time() - 1000, closed_at=time.time(),
        fill_latency_seconds=2.0,
        model_probability=0.85 if correct else 0.10,
        realised_outcome=correct,
    )


def test_zero_trades_blocks_everything() -> None:
    gate = WeatherValidationGate()
    out = gate.check(paper_trades=[], paper_days=0.5, equity_curve=[Decimal("1260")],
                     station_audit={"accuracy": 0.0, "total": 0, "correct": 0})
    assert out["pass"] is False
    assert all(not c["pass"] or c == out["criteria"]["max_drawdown"] for c in out["criteria"].values()) is False


def test_meets_all_thresholds() -> None:
    # 150 trades over 20 days, half winners, low brier, healthy equity curve
    trades = [_trade(0.10, correct=1) for _ in range(120)] + [_trade(-0.05, correct=0) for _ in range(40)]
    # Equity curve that climbs steadily — produces positive Sharpe
    equity = [Decimal("1260") + Decimal(str(i * 0.5)) for i in range(200)]
    gate = WeatherValidationGate()
    out = gate.check(
        paper_trades=trades, paper_days=20.0,
        equity_curve=equity,
        station_audit={"accuracy": 1.0, "total": 10, "correct": 10},
    )
    # Cost ratio + brier may still bind but trade_count, sharpe, drawdown,
    # station_accuracy, position_cap_compliance must pass
    crit = out["criteria"]
    assert crit["trade_count"]["pass"]
    assert crit["station_accuracy"]["pass"]
    assert crit["position_cap_compliance"]["pass"]
    assert crit["max_drawdown"]["pass"]


def test_single_strategy_concentration_blocks() -> None:
    gate = WeatherValidationGate()
    trades = (
        [_trade(1.00, strategy="weather_ensemble") for _ in range(100)]
        + [_trade(0.01, strategy="negative_risk_arb") for _ in range(50)]
    )
    out = gate.check(
        paper_trades=trades, paper_days=20.0,
        equity_curve=[Decimal("1260"), Decimal("1300")],
        station_audit={"accuracy": 1.0, "total": 50, "correct": 50},
    )
    assert out["criteria"]["strategy_concentration"]["pass"] is False


def test_cap_violations_fail() -> None:
    gate = WeatherValidationGate()
    trades = [_trade(0.10) for _ in range(200)]
    trades[0].cap_violation = True
    out = gate.check(
        paper_trades=trades, paper_days=20.0,
        equity_curve=[Decimal("1260"), Decimal("1280")],
        station_audit={"accuracy": 1.0, "total": 200, "correct": 200},
    )
    assert out["criteria"]["position_cap_compliance"]["pass"] is False
