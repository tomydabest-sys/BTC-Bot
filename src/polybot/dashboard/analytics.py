"""Trade analytics — computes edge, signal quality, and risk metrics.

This module reads from the bot's SQLite database and computes the metrics
the dashboard needs to evaluate strategy performance over time.

Drop this in src/polybot/dashboard/analytics.py and import it from app.py.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any


@dataclass
class TradePair:
    """A complete round-trip trade (entry + exit)."""
    market_id: str
    strategy: str
    side: str  # BUY or SELL (entry side)
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    entry_time: float
    exit_time: float
    hold_seconds: float
    exit_reason: str  # "stop_loss", "auto_close", "target", "unknown"


# ─────────────────────────────────────────────────────────────────────────────
#  RAW DATA LOADERS
# ─────────────────────────────────────────────────────────────────────────────


def _load_orders(db_path: str, limit: int = 5000) -> list[dict]:
    """Load all orders from SQLite, newest first."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def _pair_trades(orders: list[dict]) -> list[TradePair]:
    """Match entry and exit orders into round-trip trades.

    An exit is identified by strategy starting with 'exit_' or 'auto_exit'.
    Pairs are matched by market_id in chronological order.
    """
    # Sort oldest first so we can match in order
    orders_sorted = sorted(orders, key=lambda o: o.get("created_at", ""))

    open_positions: dict[str, list[dict]] = defaultdict(list)
    pairs: list[TradePair] = []

    for o in orders_sorted:
        if not o.get("filled_size") or o["filled_size"] <= 0:
            continue

        market_id = o["market_id"]
        strategy = o.get("strategy", "")
        is_exit = strategy.startswith("exit_") or strategy.startswith("auto_exit")

        if not is_exit:
            # This is an entry — push onto the stack for this market
            open_positions[market_id].append(o)
        else:
            # This is an exit — match against the most recent entry
            if not open_positions[market_id]:
                continue
            entry = open_positions[market_id].pop(0)

            try:
                entry_time = _parse_time(entry["created_at"])
                exit_time = _parse_time(o["created_at"])
                entry_price = float(entry.get("avg_fill_price") or entry.get("price", 0))
                exit_price = float(o.get("avg_fill_price") or o.get("price", 0))
                size = float(entry.get("filled_size") or entry.get("size", 0))
                entry_side = entry.get("side", "BUY")

                # P&L: BUY = profit when exit > entry, SELL = profit when exit < entry
                if entry_side == "BUY":
                    pnl = (exit_price - entry_price) * size
                else:
                    pnl = (entry_price - exit_price) * size

                pnl_pct = (pnl / (entry_price * size)) if entry_price * size > 0 else 0

                # Determine exit reason
                if "auto_exit" in strategy:
                    reason = "auto_close"
                elif "exit_" in strategy:
                    reason = "stop_loss"
                else:
                    reason = "unknown"

                pairs.append(TradePair(
                    market_id=market_id,
                    strategy=entry.get("strategy", "unknown"),
                    side=entry_side,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    size=size,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    entry_time=entry_time,
                    exit_time=exit_time,
                    hold_seconds=exit_time - entry_time,
                    exit_reason=reason,
                ))
            except (ValueError, KeyError, TypeError):
                continue

    return pairs


def _parse_time(s: str) -> float:
    """Parse an ISO timestamp string to a unix timestamp."""
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


# ─────────────────────────────────────────────────────────────────────────────
#  EDGE METRICS
# ─────────────────────────────────────────────────────────────────────────────


def compute_edge_metrics(pairs: list[TradePair]) -> dict[str, Any]:
    """The 'does this strategy actually have edge?' metrics.

    Returns: total_trades, win_rate, avg_win, avg_loss, profit_factor,
    expectancy, sharpe-like ratio, best/worst trade, total_pnl.
    """
    if not pairs:
        return {
            "total_trades": 0, "wins": 0, "losses": 0, "breakeven": 0,
            "win_rate": 0, "avg_win": 0, "avg_loss": 0,
            "profit_factor": 0, "expectancy": 0,
            "best_trade": 0, "worst_trade": 0, "total_pnl": 0,
            "avg_hold_seconds": 0, "sharpe_like": 0,
        }

    wins = [p for p in pairs if p.pnl > 0.001]
    losses = [p for p in pairs if p.pnl < -0.001]
    breakeven = len(pairs) - len(wins) - len(losses)

    total_pnl = sum(p.pnl for p in pairs)
    gross_wins = sum(p.pnl for p in wins)
    gross_losses = abs(sum(p.pnl for p in losses))

    win_rate = len(wins) / len(pairs) if pairs else 0
    avg_win = gross_wins / len(wins) if wins else 0
    avg_loss = gross_losses / len(losses) if losses else 0
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (
        float("inf") if gross_wins > 0 else 0
    )
    expectancy = total_pnl / len(pairs) if pairs else 0

    # Sharpe-like: mean P&L / std dev of P&L
    if len(pairs) > 1:
        pnls = [p.pnl for p in pairs]
        std = statistics.stdev(pnls)
        sharpe_like = (statistics.mean(pnls) / std) if std > 0 else 0
    else:
        sharpe_like = 0

    return {
        "total_trades": len(pairs),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": breakeven,
        "win_rate": round(win_rate * 100, 2),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else 999,
        "expectancy": round(expectancy, 4),
        "best_trade": round(max((p.pnl for p in pairs), default=0), 4),
        "worst_trade": round(min((p.pnl for p in pairs), default=0), 4),
        "total_pnl": round(total_pnl, 4),
        "gross_wins": round(gross_wins, 4),
        "gross_losses": round(gross_losses, 4),
        "avg_hold_seconds": round(
            statistics.mean(p.hold_seconds for p in pairs) if pairs else 0, 1
        ),
        "sharpe_like": round(sharpe_like, 3),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  SIGNAL ANALYSIS — buckets by entry price, etc.
# ─────────────────────────────────────────────────────────────────────────────


def _bucket_stats(pairs: list[TradePair], key_fn) -> list[dict]:
    """Group pairs by a key function and compute win rate per bucket."""
    buckets: dict[str, list[TradePair]] = defaultdict(list)
    for p in pairs:
        try:
            key = key_fn(p)
            if key is not None:
                buckets[key].append(p)
        except Exception:
            continue

    result = []
    for key, group in buckets.items():
        wins = sum(1 for p in group if p.pnl > 0.001)
        total_pnl = sum(p.pnl for p in group)
        result.append({
            "bucket": key,
            "trades": len(group),
            "wins": wins,
            "win_rate": round((wins / len(group)) * 100, 1) if group else 0,
            "total_pnl": round(total_pnl, 4),
            "avg_pnl": round(total_pnl / len(group), 4) if group else 0,
        })
    return sorted(result, key=lambda x: x["bucket"])


def compute_signal_analysis(pairs: list[TradePair]) -> dict[str, Any]:
    """Bucket trades by various dimensions to find what predicts winners."""

    def entry_price_bucket(p: TradePair) -> str:
        ep = p.entry_price
        if ep < 0.30: return "0.20-0.30"
        if ep < 0.40: return "0.30-0.40"
        if ep < 0.50: return "0.40-0.50"
        if ep < 0.60: return "0.50-0.60"
        if ep < 0.70: return "0.60-0.70"
        return "0.70+"

    def hold_time_bucket(p: TradePair) -> str:
        h = p.hold_seconds
        if h < 30: return "<30s"
        if h < 60: return "30-60s"
        if h < 120: return "1-2m"
        if h < 300: return "2-5m"
        return ">5m"

    def side_bucket(p: TradePair) -> str:
        return p.side

    def strategy_bucket(p: TradePair) -> str:
        return p.strategy

    def exit_reason_bucket(p: TradePair) -> str:
        return p.exit_reason

    return {
        "by_entry_price": _bucket_stats(pairs, entry_price_bucket),
        "by_hold_time": _bucket_stats(pairs, hold_time_bucket),
        "by_side": _bucket_stats(pairs, side_bucket),
        "by_strategy": _bucket_stats(pairs, strategy_bucket),
        "by_exit_reason": _bucket_stats(pairs, exit_reason_bucket),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  RISK METRICS — drawdown, streaks, exposure
# ─────────────────────────────────────────────────────────────────────────────


def compute_risk_metrics(pairs: list[TradePair]) -> dict[str, Any]:
    """Drawdown, losing streaks, recovery time, etc."""
    if not pairs:
        return {
            "max_drawdown": 0, "max_drawdown_pct": 0,
            "current_drawdown": 0, "longest_losing_streak": 0,
            "longest_winning_streak": 0, "current_streak": 0,
            "current_streak_type": "none",
            "equity_curve": [],
            "trades_per_hour": 0,
        }

    # Sort chronologically
    sorted_pairs = sorted(pairs, key=lambda p: p.exit_time)

    # Build equity curve
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_pct = 0.0
    curve = []

    for p in sorted_pairs:
        equity += p.pnl
        if equity > peak:
            peak = equity
        dd = peak - equity
        dd_pct = (dd / peak * 100) if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
        curve.append({
            "t": int(p.exit_time),
            "equity": round(equity, 4),
            "drawdown": round(dd, 4),
        })

    current_drawdown = peak - equity

    # Streaks
    longest_win = 0
    longest_loss = 0
    cur_streak = 0
    cur_type = "none"

    for p in sorted_pairs:
        if p.pnl > 0.001:
            if cur_type == "win":
                cur_streak += 1
            else:
                cur_streak = 1
                cur_type = "win"
            longest_win = max(longest_win, cur_streak)
        elif p.pnl < -0.001:
            if cur_type == "loss":
                cur_streak += 1
            else:
                cur_streak = 1
                cur_type = "loss"
            longest_loss = max(longest_loss, cur_streak)

    # Trades per hour
    if len(sorted_pairs) >= 2:
        time_span = sorted_pairs[-1].exit_time - sorted_pairs[0].entry_time
        tph = (len(sorted_pairs) / time_span * 3600) if time_span > 0 else 0
    else:
        tph = 0

    return {
        "max_drawdown": round(max_dd, 4),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "current_drawdown": round(current_drawdown, 4),
        "longest_losing_streak": longest_loss,
        "longest_winning_streak": longest_win,
        "current_streak": cur_streak,
        "current_streak_type": cur_type,
        "equity_curve": curve[-200:],  # Last 200 points
        "trades_per_hour": round(tph, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  TIME-BUCKETED PERFORMANCE
# ─────────────────────────────────────────────────────────────────────────────


def compute_time_buckets(pairs: list[TradePair]) -> dict[str, Any]:
    """Performance broken down by time windows."""
    if not pairs:
        return {"last_hour": {}, "last_day": {}, "last_week": {}, "all_time": {}}

    now = datetime.utcnow().timestamp()

    def filter_window(seconds: int) -> list[TradePair]:
        return [p for p in pairs if (now - p.exit_time) <= seconds]

    return {
        "last_hour": compute_edge_metrics(filter_window(3600)),
        "last_day": compute_edge_metrics(filter_window(86400)),
        "last_week": compute_edge_metrics(filter_window(604800)),
        "all_time": compute_edge_metrics(pairs),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────


def get_full_analytics(db_path: str = "./data/bot.db") -> dict[str, Any]:
    """Returns the complete analytics payload for the dashboard."""
    orders = _load_orders(db_path, limit=5000)
    pairs = _pair_trades(orders)

    return {
        "edge": compute_edge_metrics(pairs),
        "signal_analysis": compute_signal_analysis(pairs),
        "risk": compute_risk_metrics(pairs),
        "time_buckets": compute_time_buckets(pairs),
        "recent_trades": [
            {
                "market_id": p.market_id[:16],
                "strategy": p.strategy,
                "side": p.side,
                "entry_price": round(p.entry_price, 4),
                "exit_price": round(p.exit_price, 4),
                "size": round(p.size, 2),
                "pnl": round(p.pnl, 4),
                "pnl_pct": round(p.pnl_pct * 100, 2),
                "hold_seconds": round(p.hold_seconds, 1),
                "exit_reason": p.exit_reason,
                "exit_time": int(p.exit_time),
            }
            for p in sorted(pairs, key=lambda x: x.exit_time, reverse=True)[:50]
        ],
    }
