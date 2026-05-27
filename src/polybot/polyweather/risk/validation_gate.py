"""Weather paper-validation gate — 9 criteria.

All 9 must be True for live trading to be permitted. The check is read-only;
the orchestrator calls it on startup and dashboard surfaces it at
``/api/weather/validation-gate``.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class TradePair:
    """Closed paper trade — entry + exit fully resolved."""

    market_id: str
    event_id: str
    strategy: str
    station: str
    city: str
    side: str
    entry_price: Decimal
    exit_price: Decimal
    size: Decimal
    fees_usdc: Decimal
    rebates_usdc: Decimal
    realised_pnl_usdc: Decimal
    opened_at: float
    closed_at: float
    fill_latency_seconds: float
    model_probability: float
    realised_outcome: int  # 1 if YES resolved true, 0 if false
    used_dynamic_fee: bool = True
    cap_violation: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def _brier(trades: list[TradePair]) -> float:
    if not trades:
        return 1.0
    return sum((t.model_probability - t.realised_outcome) ** 2 for t in trades) / len(trades)


def _max_drawdown(equity: list[Decimal]) -> Decimal:
    if not equity:
        return Decimal("0")
    peak = equity[0]
    max_dd = Decimal("0")
    for x in equity:
        if x > peak:
            peak = x
        if peak > 0:
            dd = (peak - x) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _sharpe_annualised(equity: list[Decimal]) -> float:
    if len(equity) < 2:
        return 0.0
    returns: list[float] = []
    for prev, cur in zip(equity, equity[1:], strict=False):
        if prev <= 0:
            continue
        returns.append(float((cur - prev) / prev))
    if len(returns) < 2:
        return 0.0
    mean_r = statistics.fmean(returns)
    sd_r = statistics.pstdev(returns)
    if sd_r == 0:
        return 0.0
    return (mean_r / sd_r) * math.sqrt(365)


def _fill_rate(trades: list[TradePair], window_seconds: float = 6 * 3600) -> float:
    if not trades:
        return 0.0
    filled = sum(1 for t in trades if 0 < t.fill_latency_seconds <= window_seconds)
    return filled / len(trades)


def _max_strategy_pl_share(trades: list[TradePair]) -> float:
    if not trades:
        return 0.0
    pnl_by: dict[str, Decimal] = {}
    for t in trades:
        pnl_by[t.strategy] = pnl_by.get(t.strategy, Decimal("0")) + t.realised_pnl_usdc
    pos = {k: v for k, v in pnl_by.items() if v > 0}
    total_pos = sum(pos.values(), start=Decimal("0"))
    if total_pos <= 0:
        return 0.0
    return float(max(pos.values()) / total_pos)


def _cost_ratio(trades: list[TradePair]) -> float:
    if not trades:
        return 0.0
    gross = sum(t.realised_pnl_usdc + t.fees_usdc - t.rebates_usdc for t in trades)
    costs = sum(t.fees_usdc - t.rebates_usdc for t in trades)
    if gross <= 0:
        return 1.0  # treat as failing
    return float(costs / gross)


def _cap_violations(trades: list[TradePair]) -> int:
    return sum(1 for t in trades if t.cap_violation)


class WeatherValidationGate:
    """9-criterion check. Each criterion returns value, threshold, pass, reason."""

    DEFAULT_THRESHOLDS = {
        "trade_count": 100,
        "paper_days": 14,
        "brier_max": 0.18,
        "sharpe_min": 1.0,
        "max_drawdown_max": Decimal("0.15"),
        "fill_rate_min": 0.40,
        "strategy_concentration_max": 0.60,
        "station_accuracy_min": 1.0,
        "cost_ratio_max": 0.25,
        "cap_violations_max": 0,
    }

    def __init__(self, thresholds: dict[str, Any] | None = None) -> None:
        self.thresholds = dict(self.DEFAULT_THRESHOLDS)
        if thresholds:
            self.thresholds.update(thresholds)

    def check(
        self,
        paper_trades: list[TradePair],
        paper_days: float,
        equity_curve: list[Decimal],
        station_audit: dict[str, Any],
    ) -> dict[str, Any]:
        criteria: dict[str, dict[str, Any]] = {}

        # 1. Minimum trade count + 14 days
        cond1_pass = (
            len(paper_trades) >= self.thresholds["trade_count"]
            and paper_days >= self.thresholds["paper_days"]
        )
        criteria["trade_count"] = {
            "value": len(paper_trades),
            "threshold": self.thresholds["trade_count"],
            "pass": cond1_pass,
            "reason": f"{len(paper_trades)} trades over {paper_days:.1f} days",
        }

        # 2. Brier
        brier = _brier(paper_trades)
        criteria["brier_score"] = {
            "value": brier,
            "threshold": self.thresholds["brier_max"],
            "pass": brier <= self.thresholds["brier_max"],
            "reason": f"Brier={brier:.4f}",
        }

        # 3. Sharpe
        sharpe = _sharpe_annualised(equity_curve)
        criteria["sharpe"] = {
            "value": sharpe,
            "threshold": self.thresholds["sharpe_min"],
            "pass": sharpe >= self.thresholds["sharpe_min"],
            "reason": f"Sharpe={sharpe:.3f}",
        }

        # 4. Max drawdown
        max_dd = _max_drawdown(equity_curve)
        criteria["max_drawdown"] = {
            "value": float(max_dd),
            "threshold": float(self.thresholds["max_drawdown_max"]),
            "pass": max_dd <= Decimal(str(self.thresholds["max_drawdown_max"])),
            "reason": f"max_dd={max_dd:.4f}",
        }

        # 5. Fill rate
        fill_rate = _fill_rate(paper_trades)
        criteria["fill_rate"] = {
            "value": fill_rate,
            "threshold": self.thresholds["fill_rate_min"],
            "pass": fill_rate >= self.thresholds["fill_rate_min"],
            "reason": f"fill={fill_rate:.2%}",
        }

        # 6. Strategy concentration
        max_share = _max_strategy_pl_share(paper_trades)
        criteria["strategy_concentration"] = {
            "value": max_share,
            "threshold": self.thresholds["strategy_concentration_max"],
            "pass": max_share <= self.thresholds["strategy_concentration_max"],
            "reason": f"max_share={max_share:.2%}",
        }

        # 7. Station accuracy
        accuracy = float(station_audit.get("accuracy", 0.0))
        criteria["station_accuracy"] = {
            "value": accuracy,
            "threshold": self.thresholds["station_accuracy_min"],
            "pass": accuracy >= self.thresholds["station_accuracy_min"],
            "reason": f"{station_audit.get('correct', 0)}/{station_audit.get('total', 0)} verified",
        }

        # 8. Cost ratio
        cost_ratio = _cost_ratio(paper_trades)
        criteria["cost_ratio"] = {
            "value": cost_ratio,
            "threshold": self.thresholds["cost_ratio_max"],
            "pass": cost_ratio < self.thresholds["cost_ratio_max"],
            "reason": f"costs/gross={cost_ratio:.2%}",
        }

        # 9. Cap-violation count
        vio = _cap_violations(paper_trades)
        criteria["position_cap_compliance"] = {
            "value": vio,
            "threshold": self.thresholds["cap_violations_max"],
            "pass": vio <= self.thresholds["cap_violations_max"],
            "reason": f"{vio} violations",
        }

        all_pass = all(c["pass"] for c in criteria.values())
        return {"pass": all_pass, "criteria": criteria}
