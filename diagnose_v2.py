"""BTC-Bot v2 diagnostic — single script to pinpoint why no trades.

Usage (from repo root):
    python diagnose_v2.py

Run while bot is RUNNING. Paste full output back to chat.
"""

import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


def hr(label: str) -> None:
    print()
    print("=" * 72)
    print(f"  {label}")
    print("=" * 72)


def section(label: str) -> None:
    print()
    print(f"[{label}]")
    print("-" * 72)


hr("BTC-BOT v2 DIAGNOSTIC")
print(f"  Time: {datetime.now().isoformat()}")
print(f"  CWD:  {Path.cwd()}")

# ─────────────────────────────────────────────────────────────────────────
# 1. Bot.db orders
# ─────────────────────────────────────────────────────────────────────────
section("1. ORDERS IN bot.db")
db_path = Path("data/bot.db")
if not db_path.exists():
    print(f"  [!] {db_path} not found")
else:
    try:
        c = sqlite3.connect(str(db_path))
        total = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        print(f"  Total orders: {total}")

        rows = c.execute("""
            SELECT strategy, status, COUNT(*) as n,
                   SUM(CASE WHEN filled_size > 0 THEN 1 ELSE 0 END) as filled
            FROM orders
            GROUP BY strategy, status
            ORDER BY n DESC
        """).fetchall()
        print("\n  By strategy x status:")
        print(f"    {'strategy':<30} {'status':<12} {'count':<8} {'filled':<8}")
        for strategy, status, n, filled in rows:
            print(f"    {(strategy or '?'):<30} {(status or '?'):<12} "
                  f"{n:<8} {filled or 0:<8}")

        cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
        recent = c.execute(
            "SELECT COUNT(*) FROM orders WHERE created_at > ?", (cutoff,)
        ).fetchone()[0]
        print(f"\n  Last 1 hour:  {recent} orders created")

        cutoff5m = (datetime.now() - timedelta(minutes=5)).isoformat()
        recent5m = c.execute(
            "SELECT COUNT(*) FROM orders WHERE created_at > ?", (cutoff5m,)
        ).fetchone()[0]
        print(f"  Last 5 min:   {recent5m} orders created")

        latest = c.execute(
            "SELECT created_at, strategy, side, price, size, status, filled_size "
            "FROM orders ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        print("\n  5 most recent orders:")
        for r in latest:
            print(f"    {r[0][:19]} {r[1]:<25} {r[2]:<5} px={r[3]:<6} "
                  f"sz={r[4]:<8.2f} {r[5]:<10} fill={r[6]}")
        c.close()
    except Exception as e:
        print(f"  [!] Error reading bot.db: {e}")

# ─────────────────────────────────────────────────────────────────────────
# 2. Decision log
# ─────────────────────────────────────────────────────────────────────────
section("2. DECISION LOG — last 5 minutes")
dec_paths = sorted(
    Path("logs").glob("decisions*.jsonl"),
    key=lambda p: p.stat().st_mtime,
    reverse=True,
)
if not dec_paths:
    print("  [!] No decisions.jsonl found in logs/")
else:
    cutoff_ts = time.time() - 300
    block_counts: Counter = Counter()
    decision_counts: Counter = Counter()
    strategy_counts: Counter = Counter()
    strategy_decisions: dict[str, Counter] = {}
    total = 0

    for p in dec_paths[:3]:
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = d.get("ts", 0)
                    if ts < cutoff_ts:
                        continue
                    total += 1
                    decision = d.get("decision", "?")
                    decision_counts[decision] += 1
                    strategy = d.get("strategy", "?")
                    strategy_counts[strategy] += 1
                    if strategy not in strategy_decisions:
                        strategy_decisions[strategy] = Counter()
                    strategy_decisions[strategy][decision] += 1
                    if decision == "BLOCKED":
                        block_counts[d.get("reason", "?")] += 1
        except Exception as e:
            print(f"  [!] Error reading {p.name}: {e}")

    print(f"  Total decisions in last 5 min: {total}")

    if total > 0:
        print("\n  By decision:")
        for k, v in decision_counts.most_common():
            pct = v / total * 100
            print(f"    {k:<20} {v:<8} ({pct:.1f}%)")

        print("\n  Per-strategy decision mix:")
        for s, dc in sorted(strategy_decisions.items(), key=lambda x: -sum(x[1].values()))[:8]:
            tot = sum(dc.values())
            ok = dc.get("BUY", 0) + dc.get("SELL", 0)
            blk = dc.get("BLOCKED", 0)
            ok_pct = ok / tot * 100 if tot else 0
            print(f"    {s:<28} total={tot:<6} ok={ok:<5} ({ok_pct:.1f}%) blocked={blk}")

        if block_counts:
            print("\n  Top block reasons:")
            total_blocks = sum(block_counts.values())
            for k, v in block_counts.most_common(15):
                pct = v / total_blocks * 100
                print(f"    {k:<35} {v:<8} ({pct:.1f}%)")

# ─────────────────────────────────────────────────────────────────────────
# 3. Recent log activity
# ─────────────────────────────────────────────────────────────────────────
section("3. RECENT LOG ACTIVITY")
log_path = Path("logs/polybot.log")
if not log_path.exists():
    alts = list(Path("logs").glob("*.log"))
    log_path = alts[0] if alts else None

if log_path and log_path.exists():
    try:
        with open(log_path, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-300:]

        feed_diag_lines = [l for l in lines if "feed_diag" in l]
        if feed_diag_lines:
            print(f"  Most recent feed_diag (truncated to 280 chars):")
            print(f"    {feed_diag_lines[-1].strip()[:280]}")
        else:
            print("  [!] No feed_diag line in last 300 lines")

        scan_lines = [l for l in lines if "scan_complete" in l]
        if scan_lines:
            print(f"\n  Most recent scan_complete:")
            print(f"    {scan_lines[-1].strip()[:200]}")

        keywords = [
            "maker_signal",
            "overshoot_signal",
            "boundary_signal",
            "paper_fill",
            "exec_blocked",
            "position_opened",
            "ws_silent_freeze",
            "ws_force_reconnect",
            "ws_book_event_milestone",
            "ws_connected",
            "ws_subscribed_batch",
            "feed_stale_skip_cycle",
            "no_snapshot",
            "auto_close",
            "force_close",
            "market_expired_force_close",
        ]
        signal_counts: Counter = Counter()
        for line in lines:
            for kw in keywords:
                if kw in line:
                    signal_counts[kw] += 1

        print(f"\n  Last 300 log-line event counts:")
        for kw in keywords:
            n = signal_counts.get(kw, 0)
            marker = "  ←" if n > 0 else ""
            print(f"    {kw:<32} {n}{marker}")

        error_lines = [
            l for l in lines
            if " error" in l.lower() or "exception" in l.lower()
        ]
        if error_lines:
            print(f"\n  Errors (last 5 of {len(error_lines)}):")
            for l in error_lines[-5:]:
                print(f"    {l.strip()[:220]}")
        else:
            print("\n  No errors in last 300 lines")
    except Exception as e:
        print(f"  [!] Error reading log: {e}")
else:
    print(f"  [!] logs/polybot.log not found")

# ─────────────────────────────────────────────────────────────────────────
# 4. Patch verification
# ─────────────────────────────────────────────────────────────────────────
section("4. V2 PATCH VERIFICATION")
checks = [
    ("src/polybot/strategies/maker_edge.py", "max_position_notional_usd"),
    ("src/polybot/strategies/maker_edge.py", "min_quote_interval_s"),
    ("src/polybot/risk/manager.py", "_is_exit_order"),
    ("src/polybot/data/websocket.py", "MAX_TOKENS_PER_SUBSCRIBE"),
    ("src/polybot/data/websocket.py", "_send_subscribe_batch"),
    ("src/polybot/main.py", "_force_closed_markets"),
    ("src/polybot/main.py", "market_expired_force_close"),
]
for path, marker in checks:
    p = Path(path)
    if not p.exists():
        print(f"  [MISSING FILE]    {path}")
        continue
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
        ok = marker in text
        flag = "[v2 OK]      " if ok else "[v1 OR BAD]  "
        print(f"  {flag}{path}  ::  '{marker}'")
    except Exception as e:
        print(f"  [READ ERR]   {path}: {e}")

# ─────────────────────────────────────────────────────────────────────────
# 5. Config check
# ─────────────────────────────────────────────────────────────────────────
section("5. ACTIVE CONFIG SUMMARY")
import re
for cfg_name in ["config.aggressive.yaml", "config.yaml"]:
    p = Path(cfg_name)
    if not p.exists():
        continue
    try:
        text = p.read_text(encoding="utf-8")
        # Quick grep for key params
        keys = ["bankroll_usd", "min_usd", "kelly_fraction", "edge_floor_bps",
                "min_spread", "confidence_floor", "min_quote_interval_s",
                "max_time_remaining", "min_overshoot", "loop_interval_ms",
                "force_trade"]
        print(f"\n  {cfg_name}:")
        for key in keys:
            matches = re.findall(rf"^[^#]*\b{key}\s*:\s*([\S]+)", text, re.M)
            if matches:
                print(f"    {key:<28} {matches[0]}")
    except Exception as e:
        print(f"    [!] {cfg_name}: {e}")

# ─────────────────────────────────────────────────────────────────────────
# 6. Environment
# ─────────────────────────────────────────────────────────────────────────
section("6. ENVIRONMENT")
import os
for var in ["BOT_FORCE_TRADE", "BOT_MODE", "BOT_LOG_LEVEL"]:
    val = os.environ.get(var, "(unset)")
    print(f"  {var:<25} = {val}")

hr("END DIAGNOSTIC — paste this entire output back to chat")
