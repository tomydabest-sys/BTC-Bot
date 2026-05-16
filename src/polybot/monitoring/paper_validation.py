"""Paper-validation gate — Phase 4 of the V2 brief.

Tracks the 9 pass/fail metrics that must ALL clear before the operator is
allowed to flip the bot from paper to live. The gate is intentionally
strict: if even one metric fails, the gate returns NOT_READY and the live
deployment is blocked.

Metrics (per the brief):

  | Metric                       | Threshold            |
  |------------------------------|----------------------|
  | Duration                     | >= 30 days           |
  | Paper trades (round trips)   | >= 500               |
  | Net P&L (after fees)         | Positive             |
  | Sharpe ratio (daily)         | >= 1.5               |
  | Max drawdown                 | < 15%                |
  | Quote uptime                 | > 80%                |
  | Cancel/replace p95           | < 150ms              |
  | Unhandled exceptions         | == 0                 |
  | Fee-rate consistency         | 100% dynamic fetch   |

The gate persists its state to a JSON file so a bot restart doesn't
reset the 30-day clock.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
#  Pass/fail thresholds
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class GateThresholds:
    min_duration_days: float = 30.0
    min_trades: int = 500
    min_net_pnl_usd: float = 0.0
    min_sharpe_daily: float = 1.5
    max_drawdown_pct: float = 0.15
    min_quote_uptime_pct: float = 80.0
    max_p95_latency_ms: float = 150.0
    max_unhandled_exceptions: int = 0
    min_fee_consistency_pct: float = 100.0


# ─────────────────────────────────────────────────────────────────────────────
#  State (serialisable to JSON)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ValidationState:
    """Snapshot of every metric the gate evaluates."""

    started_at: float = 0.0          # epoch seconds (UTC)
    last_updated: float = 0.0
    trade_count: int = 0
    gross_pnl_usd: float = 0.0
    fees_paid_usd: float = 0.0
    rebates_earned_usd: float = 0.0
    daily_returns_pct: list[float] = field(default_factory=list)  # rolling daily
    equity_curve: list[float] = field(default_factory=list)       # USD timeline
    peak_equity_usd: float = 0.0
    last_p95_latency_ms: float = 0.0
    quote_uptime_pct: float = 0.0
    unhandled_exceptions: int = 0
    orders_total: int = 0
    orders_with_dynamic_fee: int = 0

    @property
    def net_pnl_usd(self) -> float:
        return self.gross_pnl_usd - self.fees_paid_usd + self.rebates_earned_usd

    @property
    def duration_days(self) -> float:
        if self.started_at <= 0:
            return 0.0
        return (time.time() - self.started_at) / 86400.0

    @property
    def current_equity_usd(self) -> float:
        return self.equity_curve[-1] if self.equity_curve else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for v in self.equity_curve:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    @property
    def sharpe_daily(self) -> float:
        if len(self.daily_returns_pct) < 2:
            return 0.0
        try:
            mean = statistics.mean(self.daily_returns_pct)
            stdev = statistics.stdev(self.daily_returns_pct)
        except statistics.StatisticsError:
            return 0.0
        if stdev <= 0 or not math.isfinite(stdev):
            return 0.0
        # Daily Sharpe — annualisation is irrelevant to the gate check.
        return mean / stdev

    @property
    def fee_consistency_pct(self) -> float:
        if self.orders_total <= 0:
            return 100.0  # no orders to violate the rule
        return 100.0 * self.orders_with_dynamic_fee / self.orders_total


# ─────────────────────────────────────────────────────────────────────────────
#  Per-metric verdict
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class MetricVerdict:
    name: str
    value: float
    threshold: float
    passed: bool
    comparator: str  # "gte" | "lt" | "eq"


@dataclass
class GateReport:
    status: str  # "READY" | "NOT_READY" | "INSUFFICIENT_DATA"
    metrics: list[MetricVerdict]
    failed: list[str]
    started_at_iso: str
    duration_days: float
    net_pnl_usd: float

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "metrics": [asdict(m) for m in self.metrics],
            "failed": self.failed,
            "started_at_iso": self.started_at_iso,
            "duration_days": round(self.duration_days, 2),
            "net_pnl_usd": round(self.net_pnl_usd, 2),
        }


# ─────────────────────────────────────────────────────────────────────────────
#  The gate
# ─────────────────────────────────────────────────────────────────────────────


class PaperValidationGate:
    """Tracks paper-mode metrics and evaluates pass/fail against the brief."""

    def __init__(
        self,
        *,
        state_path: str = "data/paper_validation.json",
        thresholds: GateThresholds | None = None,
        starting_equity_usd: float = 500.0,
    ) -> None:
        self._state_path = Path(state_path)
        self._thresholds = thresholds or GateThresholds()
        self._starting_equity = starting_equity_usd
        self._state = self._load() or ValidationState()
        if self._state.started_at <= 0:
            self._state.started_at = time.time()
            self._state.equity_curve = [starting_equity_usd]
            self._state.peak_equity_usd = starting_equity_usd

    # ─────────────────────────────────────────────────────────────────
    #  Recording
    # ─────────────────────────────────────────────────────────────────

    def record_round_trip(
        self,
        *,
        gross_pnl_usd: float,
        fees_paid_usd: float = 0.0,
        rebates_earned_usd: float = 0.0,
        dynamic_fee_fetched: bool = True,
    ) -> None:
        self._state.trade_count += 1
        self._state.gross_pnl_usd += gross_pnl_usd
        self._state.fees_paid_usd += fees_paid_usd
        self._state.rebates_earned_usd += rebates_earned_usd
        self._state.orders_total += 2  # round trip = 2 orders
        if dynamic_fee_fetched:
            self._state.orders_with_dynamic_fee += 2

        # Equity curve: append new mark-to-mark equity.
        new_equity = self._starting_equity + self._state.net_pnl_usd
        self._state.equity_curve.append(new_equity)
        if new_equity > self._state.peak_equity_usd:
            self._state.peak_equity_usd = new_equity
        self._state.last_updated = time.time()

    def record_daily_return(self, pct: float) -> None:
        if math.isfinite(pct):
            self._state.daily_returns_pct.append(pct)
            self._state.last_updated = time.time()

    def record_quote_uptime(self, pct: float) -> None:
        if math.isfinite(pct):
            self._state.quote_uptime_pct = max(0.0, min(100.0, pct))

    def record_p95_latency(self, ms: float) -> None:
        if math.isfinite(ms) and ms >= 0:
            self._state.last_p95_latency_ms = ms

    def record_unhandled_exception(self) -> None:
        self._state.unhandled_exceptions += 1
        self._state.last_updated = time.time()

    def reset(self) -> None:
        """Wipe state and restart the 30-day clock. Use only with intent."""
        self._state = ValidationState(
            started_at=time.time(),
            equity_curve=[self._starting_equity],
            peak_equity_usd=self._starting_equity,
        )
        self.save()

    @property
    def state(self) -> ValidationState:
        return self._state

    # ─────────────────────────────────────────────────────────────────
    #  Evaluation
    # ─────────────────────────────────────────────────────────────────

    def evaluate(self) -> GateReport:
        t = self._thresholds
        s = self._state

        metrics: list[MetricVerdict] = [
            MetricVerdict(
                name="duration_days",
                value=s.duration_days,
                threshold=t.min_duration_days,
                passed=s.duration_days >= t.min_duration_days,
                comparator="gte",
            ),
            MetricVerdict(
                name="trades",
                value=float(s.trade_count),
                threshold=float(t.min_trades),
                passed=s.trade_count >= t.min_trades,
                comparator="gte",
            ),
            MetricVerdict(
                name="net_pnl_usd",
                value=s.net_pnl_usd,
                threshold=t.min_net_pnl_usd,
                passed=s.net_pnl_usd > t.min_net_pnl_usd,
                comparator="gte",
            ),
            MetricVerdict(
                name="sharpe_daily",
                value=s.sharpe_daily,
                threshold=t.min_sharpe_daily,
                passed=s.sharpe_daily >= t.min_sharpe_daily,
                comparator="gte",
            ),
            MetricVerdict(
                name="max_drawdown_pct",
                value=s.max_drawdown_pct,
                threshold=t.max_drawdown_pct,
                passed=s.max_drawdown_pct < t.max_drawdown_pct,
                comparator="lt",
            ),
            MetricVerdict(
                name="quote_uptime_pct",
                value=s.quote_uptime_pct,
                threshold=t.min_quote_uptime_pct,
                passed=s.quote_uptime_pct > t.min_quote_uptime_pct,
                comparator="gte",
            ),
            MetricVerdict(
                name="p95_latency_ms",
                value=s.last_p95_latency_ms,
                threshold=t.max_p95_latency_ms,
                passed=s.last_p95_latency_ms < t.max_p95_latency_ms,
                comparator="lt",
            ),
            MetricVerdict(
                name="unhandled_exceptions",
                value=float(s.unhandled_exceptions),
                threshold=float(t.max_unhandled_exceptions),
                passed=s.unhandled_exceptions <= t.max_unhandled_exceptions,
                comparator="eq",
            ),
            MetricVerdict(
                name="fee_consistency_pct",
                value=s.fee_consistency_pct,
                threshold=t.min_fee_consistency_pct,
                passed=s.fee_consistency_pct >= t.min_fee_consistency_pct,
                comparator="gte",
            ),
        ]

        failed = [m.name for m in metrics if not m.passed]

        if s.trade_count == 0 and s.duration_days < 0.1:
            status = "INSUFFICIENT_DATA"
        elif failed:
            status = "NOT_READY"
        else:
            status = "READY"

        return GateReport(
            status=status,
            metrics=metrics,
            failed=failed,
            started_at_iso=datetime.fromtimestamp(
                s.started_at, tz=UTC
            ).isoformat(),
            duration_days=s.duration_days,
            net_pnl_usd=s.net_pnl_usd,
        )

    # ─────────────────────────────────────────────────────────────────
    #  Persistence
    # ─────────────────────────────────────────────────────────────────

    def save(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(asdict(self._state), indent=2))
            tmp.replace(self._state_path)
        except Exception:
            pass

    def _load(self) -> ValidationState | None:
        try:
            raw = json.loads(self._state_path.read_text())
            return ValidationState(**raw)
        except FileNotFoundError:
            return None
        except Exception:
            return None
