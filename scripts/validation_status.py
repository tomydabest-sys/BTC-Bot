"""Print a human-readable summary of the 30-day paper validation gate.

Usage:
    python scripts/validation_status.py [--state PATH] [--bankroll USD]

Exit codes:
    0  -> READY            (every gate metric passes; safe to consider live)
    1  -> NOT_READY        (one or more metrics failing)
    2  -> INSUFFICIENT_DATA (clock not started or no trades yet)

The script is read-only — it never mutates the state file.

Reminder: `LIVE_TRADING_ENABLED` in `src/polybot/data/client.py` must
NOT be flipped to True until this script reports `READY` and the
operator has reviewed the failure tail of each metric. See README.md
"Paper Validation Run".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the repo importable when running this script directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from polybot.monitoring.paper_validation import (  # noqa: E402, I001
    GateReport,
    PaperValidationGate,
)


_COMPARATOR_SYMBOL = {"gte": ">=", "lt": "<", "eq": "<="}


def _format_value(name: str, value: float) -> str:
    if name in {"net_pnl_usd"}:
        return f"${value:+.2f}"
    if name in {"max_drawdown_pct"}:
        return f"{value * 100:.2f}%"
    if name in {"quote_uptime_pct", "fee_consistency_pct"}:
        return f"{value:.2f}%"
    if name in {"p95_latency_ms"}:
        return f"{value:.1f}ms"
    if name in {"duration_days"}:
        return f"{value:.2f}d"
    if name in {"unhandled_exceptions", "trades"}:
        return f"{int(value)}"
    return f"{value:.4f}"


def _format_threshold(name: str, value: float) -> str:
    if name in {"max_drawdown_pct"}:
        return f"{value * 100:.0f}%"
    if name in {"quote_uptime_pct", "fee_consistency_pct"}:
        return f"{value:.0f}%"
    if name in {"p95_latency_ms"}:
        return f"{value:.0f}ms"
    if name in {"duration_days"}:
        return f"{value:.0f}d"
    if name in {"unhandled_exceptions", "trades"}:
        return f"{int(value)}"
    if name in {"net_pnl_usd"}:
        return f"${value:.2f}"
    return f"{value:.4f}"


def _time_to_pass(name: str, value: float, threshold: float, report: GateReport) -> str:
    """Cheap textual ETA for the time-bound metrics."""
    if name == "duration_days":
        remaining = max(0.0, threshold - value)
        if remaining <= 0:
            return "already passed"
        return f"~{remaining:.1f}d more"
    if name == "trades":
        remaining = max(0, int(threshold) - int(value))
        if remaining <= 0:
            return "already passed"
        days = max(report.duration_days, 0.01)
        rate = value / days if days > 0 else 0
        if rate <= 0:
            return f"{remaining} trades needed (no fill rate observed)"
        eta_days = remaining / rate
        return f"{remaining} more trades (~{eta_days:.1f}d at current pace)"
    return "—"


def render(report: GateReport) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("  BTC-BOT 30-DAY PAPER VALIDATION GATE")
    lines.append("=" * 72)
    lines.append(f"  Status:       {report.status}")
    lines.append(f"  Started:      {report.started_at_iso}")
    lines.append(f"  Duration:     {report.duration_days:.2f} days")
    lines.append(f"  Net P&L:      ${report.net_pnl_usd:+.2f}")
    lines.append("-" * 72)
    lines.append(
        "  Metric                       Pass  Current        Threshold      ETA"
    )
    lines.append("-" * 72)
    for m in report.metrics:
        mark = "OK  " if m.passed else "FAIL"
        cur = _format_value(m.name, m.value)
        thr = (
            _COMPARATOR_SYMBOL.get(m.comparator, "")
            + " "
            + _format_threshold(m.name, m.threshold)
        )
        eta = _time_to_pass(m.name, m.value, m.threshold, report)
        lines.append(
            f"  {m.name:<28} {mark}  {cur:<14} {thr:<14} {eta}"
        )
    lines.append("-" * 72)
    if report.failed:
        lines.append(f"  Failing: {', '.join(report.failed)}")
    lines.append(
        "  LIVE_TRADING_ENABLED must remain False until status == READY."
    )
    lines.append("=" * 72)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state",
        default="data/paper_validation.json",
        help="Path to the validation gate JSON state file.",
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        default=500.0,
        help="Starting equity used when the state file is missing.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of the text table.",
    )
    args = parser.parse_args(argv)

    gate = PaperValidationGate(
        state_path=args.state, starting_equity_usd=args.bankroll
    )
    report = gate.evaluate()

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(render(report))

    if report.status == "READY":
        return 0
    if report.status == "INSUFFICIENT_DATA":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
