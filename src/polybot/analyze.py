"""Decision-log analyzer — replaces the basic version.

PATCHED FROM ORIGINAL:
1. --since flag for time-windowed analysis (e.g. --since 5m)
2. Stage breakdown showing where each strategy dies in the pipeline
3. Health check: warns if FEED_WARMING > 50% (Cause C)
                 warns if NO_BURST > 50% with 0 book events (Cause A)
                 warns if SIZED_TO_ZERO > 30% (Cause B)
4. OK rate per strategy with trade-rate projection

Usage:
    python analyze.py
    python analyze.py --since 5m
    python analyze.py --since 1h --strategy overshoot_reversion
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

DECISIONS_PATH = Path("logs/decisions.jsonl")


def parse_since(s: str) -> float:
    """Parse '5m', '1h', '30s' into seconds."""
    s = s.strip().lower()
    if s.endswith("s"):
        return float(s[:-1])
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    if s.endswith("d"):
        return float(s[:-1]) * 86400
    return float(s)  # assume seconds


def load_decisions(since_s: float | None = None, strategy: str | None = None) -> list[dict]:
    if not DECISIONS_PATH.exists():
        print(f"[!] {DECISIONS_PATH} does not exist. Has the bot run yet?", file=sys.stderr)
        sys.exit(2)

    cutoff = time.time() - since_s if since_s else 0.0
    out = []
    with open(DECISIONS_PATH) as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("ts", 0) < cutoff:
                continue
            if strategy and d.get("strategy", "") != strategy:
                continue
            out.append(d)
    return out


def health_check(decisions: list[dict]) -> list[str]:
    """Detect known-bad patterns and surface them at the top of the report."""
    warnings: list[str] = []
    n = len(decisions)
    if n == 0:
        warnings.append("[CRITICAL] 0 decisions logged — bot likely not running or feed dead")
        return warnings

    reasons = Counter(d["reason"] for d in decisions)

    feed_warming = sum(c for r, c in reasons.items() if "feed_warming" in r.lower() or r == "ovr_01_feed_warming")
    if feed_warming / n > 0.50:
        warnings.append(
            f"[WARN] FEED_WARMING dominates ({feed_warming}/{n}={feed_warming/n:.1%}) — "
            f"likely Cause C (BTC feed unreachable). Check Test-NetConnection stream.binance.com -p 9443"
        )

    no_burst = sum(c for r, c in reasons.items() if "no_burst" in r.lower() or r == "ovr_05_no_burst")
    stale_ob = reasons.get("stale_orderbook", 0)
    if no_burst / n > 0.40 or stale_ob / n > 0.40:
        warnings.append(
            f"[WARN] NO_BURST={no_burst} STALE_ORDERBOOK={stale_ob} of {n} — "
            f"likely Cause A (Polymarket WS silent freeze). Run: "
            f"Get-Content logs/polybot.log | Select-String ws_book_event | Measure-Object"
        )

    sized_zero = sum(c for r, c in reasons.items() if r in ("sized_to_zero", "below_min_edge"))
    if sized_zero / n > 0.30:
        warnings.append(
            f"[WARN] SIZED_TO_ZERO/BELOW_MIN_EDGE={sized_zero}/{n}={sized_zero/n:.1%} — "
            f"likely Cause B (Kelly under min_usd floor). Apply risk/sizing.py min_usd=2 patch."
        )

    # `ok` now means a trade ACTUALLY executed (post aggregator/risk/exec).
    # `signal_proposed` means a strategy produced a candidate that has NOT
    # necessarily cleared the conversion gate. Distinguishing the two lets
    # us tell "no strategy is firing" apart from "strategies fire but the
    # aggregator/risk gate eats everything" — the latter looks identical
    # on the old dashboard but is a completely different bug.
    ok = reasons.get("ok", 0)
    proposed = reasons.get("signal_proposed", 0)
    if ok == 0 and proposed == 0 and n > 100:
        warnings.append(
            f"[CRITICAL] 0 OK + 0 proposed in {n} entries — no strategy is producing "
            f"signals. Engage force-trade mode: $env:BOT_FORCE_TRADE='1'"
        )
    elif ok == 0 and proposed > 0:
        warnings.append(
            f"[CRITICAL] {proposed} signals PROPOSED but 0 EXECUTED — strategies fire "
            f"but the conversion gate eats everything. Check the aggregator "
            f"min_net_score vs per-strategy weights (a lone signal needs "
            f"confidence x weight >= min_net_score), then the risk gate."
        )

    return warnings


def stage_breakdown(decisions: list[dict]) -> dict[str, dict[str, int]]:
    """Group block reasons by strategy + lifecycle stage (where they die)."""
    by_strategy_stage: dict[str, Counter] = defaultdict(Counter)
    for d in decisions:
        strat = d.get("strategy", "unknown")
        reason = d.get("reason", "unknown")
        by_strategy_stage[strat][reason] += 1
    return {k: dict(v.most_common(10)) for k, v in by_strategy_stage.items()}


def trade_rate_projection(decisions: list[dict], since_s: float | None) -> dict[str, float]:
    """Estimate trades/hour per strategy based on OK rate."""
    if not decisions:
        return {}

    span_s = since_s
    if span_s is None:
        ts = [d["ts"] for d in decisions if "ts" in d]
        if len(ts) >= 2:
            span_s = ts[-1] - ts[0]
        else:
            span_s = 1.0

    by_strat: dict[str, list[dict]] = defaultdict(list)
    for d in decisions:
        by_strat[d.get("strategy", "?")].append(d)

    out = {}
    for strat, items in by_strat.items():
        ok_count = sum(1 for d in items if d.get("reason") == "ok")
        rate_per_hour = (ok_count / span_s) * 3600 if span_s > 0 else 0
        out[strat] = round(rate_per_hour, 2)
    return out


def print_report(decisions: list[dict], since_s: float | None) -> None:
    n = len(decisions)
    print(f"\n{'=' * 70}")
    print(f"  BTC-BOT DECISION LOG ANALYSIS")
    if since_s:
        print(f"  Window: last {since_s}s ({since_s/60:.1f} min)")
    print(f"  Total decisions: {n}")
    print(f"{'=' * 70}\n")

    if n == 0:
        print("No decisions to analyze.\n")
        return

    # Health warnings
    warnings = health_check(decisions)
    if warnings:
        print("HEALTH ALERTS:")
        for w in warnings:
            print(f"  {w}")
        print()
    else:
        print("[OK] No critical patterns detected.\n")

    # Top reasons globally
    reasons = Counter(d["reason"] for d in decisions)
    print("TOP 15 BLOCK REASONS (global):")
    for reason, count in reasons.most_common(15):
        pct = count / n * 100
        bar = "#" * int(pct / 2)
        print(f"  {reason:<30} {count:>6} ({pct:5.1f}%) {bar}")
    print()

    # Per-strategy stage breakdown
    print("BY STRATEGY (top 5 reasons each):")
    breakdown = stage_breakdown(decisions)
    for strat, counts in sorted(breakdown.items()):
        total = sum(counts.values())
        print(f"\n  [{strat}]  total={total}")
        for reason, count in list(counts.items())[:5]:
            pct = count / total * 100
            print(f"    {reason:<28} {count:>5} ({pct:5.1f}%)")
    print()

    # Trade rate projection — based on EXECUTED trades (reason == "ok"),
    # not strategy proposals. This is the number that used to lie.
    rates = trade_rate_projection(decisions, since_s)
    print("EXECUTED TRADE RATE (filled trades per hour):")
    total_rate = 0.0
    for strat, rate in sorted(rates.items(), key=lambda x: -x[1]):
        if rate <= 0:
            continue
        print(f"  {strat:<30} {rate:>8.2f}/hr")
        total_rate += rate
    print(f"  {'TOTAL':<30} {total_rate:>8.2f}/hr")
    if total_rate > 0:
        hours_to_100 = 100 / total_rate
        print(f"  Hours to 100 trades: {hours_to_100:.1f}")
    print()

    # Proposed (strategy-level) vs executed (post-gate) — the gap is the
    # conversion gate's kill rate.
    executed = [d for d in decisions if d.get("reason") == "ok"]
    proposed = [d for d in decisions if d.get("reason") == "signal_proposed"]
    print(
        f"SIGNALS PROPOSED: {len(proposed)}   |   "
        f"TRADES EXECUTED: {len(executed)}"
    )
    if proposed and not executed:
        print(
            "  [!] Strategies are firing but NOTHING converts to a trade — "
            "the aggregator / risk gate is blocking 100% of signals."
        )
    print()

    # Recent executed trades
    actionable = executed
    print(f"ACTIONABLE (EXECUTED) DECISIONS: {len(actionable)} of {n} ({len(actionable)/n:.1%})")
    if actionable:
        print("\n  Last 10 executed:")
        for d in actionable[-10:]:
            ts = datetime.fromtimestamp(d.get("ts", 0)).strftime("%H:%M:%S")
            print(
                f"    {ts}  {d.get('strategy','?'):<22} "
                f"{d.get('decision','?'):<5} "
                f"mid={d.get('mid',0):.3f} "
                f"edge={d.get('edge_bps',0):>6.1f}bps "
                f"conf={d.get('confidence',0):.2f}"
            )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze decisions.jsonl")
    parser.add_argument("--since", default=None,
                        help="Only analyze decisions from last N (e.g. 5m, 1h, 30s)")
    parser.add_argument("--strategy", default=None,
                        help="Filter to one strategy")
    args = parser.parse_args()

    since_s = parse_since(args.since) if args.since else None
    decisions = load_decisions(since_s=since_s, strategy=args.strategy)
    print_report(decisions, since_s)


if __name__ == "__main__":
    main()
