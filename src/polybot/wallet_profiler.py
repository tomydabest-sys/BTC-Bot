"""Polymarket wallet profiler.

Fetches trade history for one or more wallet addresses from the Polymarket
data-api, persists to SQLite, FIFO-matches round-trips, and prints a
per-wallet metric table:

    PnL, win rate, avg hold, trade frequency, total trades, unique markets,
    maker ratio (approximation), short-window share.

Usage:
    python scripts/wallet_profiler.py
    python scripts/wallet_profiler.py 0xabc... 0xdef...
    python scripts/wallet_profiler.py --db data/wallets.db --limit 2000
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import statistics
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


DEFAULT_WALLETS = [
    "0xd84c2b6d65dc596f49c7b6aadd6d74ca91e407b9",
    "0xd0d6053c3c37e727402d84c14069780d360993aa",
    "0x63ce342161250d705dc0b16df89036c8e5f9ba9a",
    "0xeebde7a0e019a63e6b476eb425505b7b3e6eba30",
    "0xb27bc932bf8110d8f78e55da7d5f0497a18b5b82",
    "0x0006af12cd4dacc450836a0e1ec6ce47365d8c63",
    "0x04283f2fef49d70d8c55ab240450d17a65bf85b1",
    "0x89b5cdaaa4866c1e738406712012a630b4078beb",
    "0x8c901f67b036b5eebab4e1f2f904b8676743a904",
    "0x29bc82f761749e67fa00d62896bc6855097b683c",
    "0x70ec235a31eb35f243e2618d6ea3b5b8962bbb5d",
]

DATA_API_URL = "https://data-api.polymarket.com/trades"
DEFAULT_DB = "./data/wallets.db"
DEFAULT_PAGE_LIMIT = 500
DEFAULT_MAX_TRADES = 5000
HTTP_TIMEOUT = 20.0
MAX_CONCURRENT = 3


SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet_trades (
    user_addr       TEXT NOT NULL,
    tx_hash         TEXT,
    timestamp       INTEGER NOT NULL,
    condition_id    TEXT,
    asset           TEXT,
    outcome         TEXT,
    side            TEXT,
    price           REAL,
    size            REAL,
    maker           INTEGER,
    fetched_at      INTEGER NOT NULL,
    PRIMARY KEY (user_addr, tx_hash, asset, side, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_wt_user_time ON wallet_trades(user_addr, timestamp);
CREATE INDEX IF NOT EXISTS idx_wt_condition ON wallet_trades(user_addr, condition_id);
"""


@dataclass
class WalletMetrics:
    address: str
    raw_trade_count: int
    paired_round_trips: int
    unique_markets: int
    realized_pnl: float
    win_rate: float
    avg_win: float
    avg_loss: float
    avg_hold_seconds: float
    median_hold_seconds: float
    trades_per_day: float
    maker_ratio: float
    first_seen: int
    last_seen: int
    span_days: float
    short_hold_share: float


# ──────────────────────────────────────────────────────────────────
#  Fetching
# ──────────────────────────────────────────────────────────────────


async def fetch_wallet_trades(
    client: httpx.AsyncClient,
    address: str,
    max_trades: int,
    page_limit: int,
) -> list[dict]:
    """Paginate through data-api trades for a single wallet."""
    trades: list[dict] = []
    offset = 0
    while len(trades) < max_trades:
        try:
            resp = await client.get(
                DATA_API_URL,
                params={"user": address, "limit": page_limit, "offset": offset},
            )
        except Exception as e:
            print(f"  [!] fetch error for {address[:10]}: {e}", file=sys.stderr)
            break

        if resp.status_code != 200:
            print(
                f"  [!] {address[:10]} returned HTTP {resp.status_code}",
                file=sys.stderr,
            )
            break

        try:
            page = resp.json()
        except Exception:
            break

        if not isinstance(page, list) or not page:
            break

        trades.extend(page)
        if len(page) < page_limit:
            break
        offset += page_limit
        await asyncio.sleep(0.1)

    return trades[:max_trades]


# ──────────────────────────────────────────────────────────────────
#  Storage
# ──────────────────────────────────────────────────────────────────


def init_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _normalise_timestamp(raw: Any) -> int:
    if raw is None:
        return 0
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return 0
    # If the value looks like milliseconds, convert to seconds
    if v > 10_000_000_000:
        v //= 1000
    return v


def _bool_to_int(v: Any) -> int:
    if v is None:
        return 0
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(bool(v))
    if isinstance(v, str):
        return 1 if v.lower() in ("true", "1", "yes", "maker") else 0
    return 0


def persist_trades(conn: sqlite3.Connection, address: str, trades: list[dict]) -> int:
    now = int(time.time())
    rows = []
    for t in trades:
        rows.append(
            (
                address.lower(),
                t.get("transactionHash") or t.get("tx_hash") or "",
                _normalise_timestamp(t.get("timestamp") or t.get("match_time")),
                t.get("conditionId") or t.get("condition_id") or "",
                t.get("asset") or t.get("token_id") or "",
                t.get("outcome", ""),
                (t.get("side") or "").upper(),
                float(t.get("price", 0) or 0),
                float(t.get("size") or t.get("amount", 0) or 0),
                _bool_to_int(t.get("maker") or t.get("isMaker")),
                now,
            )
        )
    if not rows:
        return 0
    conn.executemany(
        "INSERT OR REPLACE INTO wallet_trades "
        "(user_addr, tx_hash, timestamp, condition_id, asset, outcome, side, "
        "price, size, maker, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def load_trades(conn: sqlite3.Connection, address: str) -> list[dict]:
    cursor = conn.execute(
        "SELECT tx_hash, timestamp, condition_id, asset, outcome, side, "
        "price, size, maker "
        "FROM wallet_trades WHERE user_addr = ? ORDER BY timestamp ASC",
        (address.lower(),),
    )
    cols = [c[0] for c in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


# ──────────────────────────────────────────────────────────────────
#  Metric computation
# ──────────────────────────────────────────────────────────────────


def compute_metrics(address: str, trades: list[dict]) -> WalletMetrics:
    if not trades:
        return WalletMetrics(
            address=address, raw_trade_count=0, paired_round_trips=0,
            unique_markets=0, realized_pnl=0.0, win_rate=0.0,
            avg_win=0.0, avg_loss=0.0, avg_hold_seconds=0.0,
            median_hold_seconds=0.0, trades_per_day=0.0, maker_ratio=0.0,
            first_seen=0, last_seen=0, span_days=0.0, short_hold_share=0.0,
        )

    trades_sorted = sorted(trades, key=lambda t: t["timestamp"])
    first_ts = trades_sorted[0]["timestamp"]
    last_ts = trades_sorted[-1]["timestamp"]
    span_seconds = max(1, last_ts - first_ts)
    span_days = span_seconds / 86400

    maker_count = sum(1 for t in trades if t.get("maker"))
    maker_ratio = maker_count / len(trades) if trades else 0

    unique_markets = len({t.get("condition_id", "") for t in trades if t.get("condition_id")})

    # FIFO per-asset pairing for round-trip PnL
    buys: dict[str, deque] = defaultdict(deque)
    realized = 0.0
    wins: list[float] = []
    losses: list[float] = []
    holds: list[float] = []
    round_trips = 0

    for t in trades_sorted:
        asset = t.get("asset") or ""
        price = float(t.get("price", 0) or 0)
        size = float(t.get("size", 0) or 0)
        ts = int(t.get("timestamp", 0))
        side = (t.get("side") or "").upper()
        if size <= 0 or price <= 0 or not asset:
            continue

        if side == "BUY":
            buys[asset].append([price, size, ts])
        elif side == "SELL":
            remaining = size
            while remaining > 0 and buys[asset]:
                entry = buys[asset][0]
                ep, es, ets = entry
                matched = min(es, remaining)
                pnl = (price - ep) * matched
                realized += pnl
                if pnl > 0:
                    wins.append(pnl)
                elif pnl < 0:
                    losses.append(pnl)
                holds.append(max(0, ts - ets))
                round_trips += 1
                entry[1] -= matched
                remaining -= matched
                if entry[1] <= 1e-9:
                    buys[asset].popleft()

    total_pairs = len(wins) + len(losses)
    win_rate = (len(wins) / total_pairs * 100) if total_pairs else 0.0
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0
    avg_hold = statistics.mean(holds) if holds else 0.0
    median_hold = statistics.median(holds) if holds else 0.0
    trades_per_day = len(trades) / span_days if span_days > 0 else 0.0

    # short_hold_share = fraction of paired round-trips held <60s (bot signature)
    short_hold_share = (
        sum(1 for h in holds if h < 60) / len(holds) * 100
        if holds else 0.0
    )

    return WalletMetrics(
        address=address,
        raw_trade_count=len(trades),
        paired_round_trips=round_trips,
        unique_markets=unique_markets,
        realized_pnl=realized,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        avg_hold_seconds=avg_hold,
        median_hold_seconds=median_hold,
        trades_per_day=trades_per_day,
        maker_ratio=maker_ratio * 100,
        first_seen=first_ts,
        last_seen=last_ts,
        span_days=span_days,
        short_hold_share=short_hold_share,
    )


# ──────────────────────────────────────────────────────────────────
#  Printing
# ──────────────────────────────────────────────────────────────────


def format_duration(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h"
    return f"{seconds/86400:.1f}d"


def print_table(metrics_list: list[WalletMetrics]) -> None:
    sorted_m = sorted(metrics_list, key=lambda m: m.realized_pnl, reverse=True)

    print()
    print("=" * 170)
    print(
        f"{'Address':<44}{'Trades':>8}{'RT':>6}{'Markets':>9}"
        f"{'PnL':>12}{'Win%':>8}{'AvgWin':>10}{'AvgLoss':>10}"
        f"{'MedHold':>10}{'AvgHold':>10}{'Trd/Day':>10}{'Maker%':>9}"
        f"{'Short%':>9}{'Span':>8}"
    )
    print("-" * 170)

    for m in sorted_m:
        print(
            f"{m.address:<44}"
            f"{m.raw_trade_count:>8}"
            f"{m.paired_round_trips:>6}"
            f"{m.unique_markets:>9}"
            f"{m.realized_pnl:>12.2f}"
            f"{m.win_rate:>8.1f}"
            f"{m.avg_win:>10.3f}"
            f"{m.avg_loss:>10.3f}"
            f"{format_duration(m.median_hold_seconds):>10}"
            f"{format_duration(m.avg_hold_seconds):>10}"
            f"{m.trades_per_day:>10.1f}"
            f"{m.maker_ratio:>8.1f}%"
            f"{m.short_hold_share:>8.1f}%"
            f"{format_duration(m.span_days * 86400):>8}"
        )
    print("=" * 170)

    # Summary fingerprint hints
    print()
    print("FINGERPRINT HINTS (bias only, not a classifier):")
    for m in sorted_m:
        hints = []
        if m.short_hold_share > 40 and m.trades_per_day > 100:
            hints.append("likely-bot")
        if m.maker_ratio > 70:
            hints.append("market-maker")
        if m.median_hold_seconds < 10 and m.raw_trade_count > 100:
            hints.append("latency-arb")
        if 30 < m.median_hold_seconds < 180 and 40 < m.win_rate < 65:
            hints.append("overshoot/mean-revert")
        if m.unique_markets > 0 and m.raw_trade_count / max(1, m.unique_markets) > 10:
            hints.append("high-turnover")
        if m.win_rate > 65 and m.avg_hold_seconds > 3600:
            hints.append("directional-hold")
        if not hints:
            hints.append("unclassified")
        print(f"  {m.address}  →  {', '.join(hints)}")
    print()


# ──────────────────────────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────────────────────────


async def _profile_one(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    address: str,
    max_trades: int,
    page_limit: int,
    use_cache: bool,
) -> WalletMetrics:
    async with sem:
        print(f"[..] fetching {address}")
        if use_cache:
            existing = load_trades(conn, address)
            if existing:
                print(f"[cache] {address[:10]} — {len(existing)} trades (cached)")
                return compute_metrics(address, existing)

        fetched = await fetch_wallet_trades(client, address, max_trades, page_limit)
        written = persist_trades(conn, address, fetched)
        print(f"[ok] {address[:10]} — {len(fetched)} trades fetched, {written} stored")
        stored = load_trades(conn, address)
        return compute_metrics(address, stored)


async def run(
    wallets: list[str],
    db_path: str,
    max_trades: int,
    page_limit: int,
    use_cache: bool,
) -> None:
    conn = init_db(db_path)
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": "btc-bot-wallet-profiler/1.0"},
    ) as client:
        tasks = [
            _profile_one(sem, client, conn, w, max_trades, page_limit, use_cache)
            for w in wallets
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    metrics: list[WalletMetrics] = []
    for r in results:
        if isinstance(r, WalletMetrics):
            metrics.append(r)
        elif isinstance(r, Exception):
            print(f"[!] task error: {r}", file=sys.stderr)

    if metrics:
        print_table(metrics)
    else:
        print("No metrics computed.")

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket wallet profiler")
    parser.add_argument(
        "wallets", nargs="*",
        help="Wallet addresses to profile (default: built-in list)",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite DB path")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_MAX_TRADES,
        help="Max trades to fetch per wallet",
    )
    parser.add_argument(
        "--page", type=int, default=DEFAULT_PAGE_LIMIT,
        help="Page size per data-api request",
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Ignore cached trades and re-fetch",
    )
    args = parser.parse_args()

    wallets = args.wallets or DEFAULT_WALLETS
    wallets = [w.strip().lower() for w in wallets if w.strip()]

    asyncio.run(
        run(
            wallets=wallets,
            db_path=args.db,
            max_trades=args.limit,
            page_limit=args.page,
            use_cache=not args.no_cache,
        )
    )


if __name__ == "__main__":
    main()
