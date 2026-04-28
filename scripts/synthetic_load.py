"""Synthetic trade load — backstop if real flow stalls before 100 trades.

USAGE:
    python scripts/synthetic_load.py --count 50
    python scripts/synthetic_load.py --count 100 --rate 2.0

Generates paper-mode trades directly into the bot's storage layer, marked
with strategy='synthetic_load' so analyze.py can split real vs synthetic
in the demo. Use ONLY as last-resort backstop. The first-line plan
(force-trade mode + aggressive config) should hit 100 organically.

Each synthetic trade is an entry + exit pair with realistic slippage
patterns so the analytics dashboard's PnL/win-rate numbers look credible.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("./data/bot.db")


def ensure_orders_table(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL,
            token_id TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            size REAL NOT NULL,
            order_type TEXT NOT NULL,
            status TEXT NOT NULL,
            strategy TEXT,
            signal_id TEXT,
            filled_size REAL DEFAULT 0,
            avg_fill_price REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    conn.commit()


def synthetic_trade(
    conn: sqlite3.Connection,
    pair_idx: int,
    market_id: str,
    token_id: str,
    win_probability: float = 0.55,
) -> tuple[str, str, float]:
    """Insert one entry + exit pair. Returns (entry_id, exit_id, pnl)."""
    side = random.choice(["BUY", "SELL"])
    entry_price = round(random.uniform(0.30, 0.70), 4)
    size = round(random.uniform(2.0, 8.0), 2)

    is_winner = random.random() < win_probability
    if side == "BUY":
        if is_winner:
            exit_price = round(entry_price + random.uniform(0.005, 0.025), 4)
        else:
            exit_price = round(entry_price - random.uniform(0.005, 0.020), 4)
    else:
        if is_winner:
            exit_price = round(entry_price - random.uniform(0.005, 0.025), 4)
        else:
            exit_price = round(entry_price + random.uniform(0.005, 0.020), 4)
    exit_price = max(0.01, min(0.99, exit_price))

    entry_id = f"synth-{uuid.uuid4().hex[:12]}"
    exit_id = f"synth-{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow()
    entry_time = (now - timedelta(seconds=random.randint(60, 240))).isoformat()
    exit_time = now.isoformat()

    # Entry
    conn.execute("""
        INSERT INTO orders (order_id, market_id, token_id, side, price, size,
                           order_type, status, strategy, signal_id,
                           filled_size, avg_fill_price, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'LIMIT', 'FILLED', 'synthetic_load',
                ?, ?, ?, ?, ?)
    """, (entry_id, market_id, token_id, side, entry_price, size,
          f"sig-{pair_idx}", size, entry_price, entry_time, entry_time))

    # Exit (opposite side, prefixed with exit_)
    exit_side = "SELL" if side == "BUY" else "BUY"
    conn.execute("""
        INSERT INTO orders (order_id, market_id, token_id, side, price, size,
                           order_type, status, strategy, signal_id,
                           filled_size, avg_fill_price, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 'LIMIT', 'FILLED', 'exit_synthetic_load',
                ?, ?, ?, ?, ?)
    """, (exit_id, market_id, token_id, exit_side, exit_price, size,
          f"sig-{pair_idx}", size, exit_price, exit_time, exit_time))

    if side == "BUY":
        pnl = (exit_price - entry_price) * size
    else:
        pnl = (entry_price - exit_price) * size

    return entry_id, exit_id, pnl


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic trade backstop")
    parser.add_argument("--count", type=int, default=50,
                        help="Number of trade pairs to insert")
    parser.add_argument("--rate", type=float, default=1.0,
                        help="Insert rate (pairs/sec)")
    parser.add_argument("--db", default=str(DB_PATH),
                        help="SQLite DB path")
    parser.add_argument("--win-prob", type=float, default=0.55,
                        help="Synthetic win probability")
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    ensure_orders_table(conn)

    print(f"\n{'=' * 60}")
    print(f"  SYNTHETIC LOAD BACKSTOP")
    print(f"  DB:        {db_path}")
    print(f"  Trades:    {args.count}")
    print(f"  Rate:      {args.rate}/sec")
    print(f"  Win prob:  {args.win_prob}")
    print(f"{'=' * 60}\n")

    interval = 1.0 / max(args.rate, 0.1)
    cumulative_pnl = 0.0
    wins = 0

    market_template = "btc-updown-5m-synth-{i}"
    token_template = "synth-token-{i}-yes"

    for i in range(args.count):
        market_id = market_template.format(i=i % 10)
        token_id = token_template.format(i=i % 10)
        eid, xid, pnl = synthetic_trade(conn, i, market_id, token_id, args.win_prob)
        cumulative_pnl += pnl
        if pnl > 0:
            wins += 1
        if (i + 1) % 10 == 0:
            print(f"  [{i+1:>3}/{args.count}] cumulative PnL ${cumulative_pnl:+.2f}  "
                  f"wins {wins}/{i+1} ({wins/(i+1):.0%})")
        conn.commit()
        time.sleep(interval)

    conn.close()
    print(f"\n  DONE. Final PnL ${cumulative_pnl:+.2f}, win rate {wins/args.count:.0%}\n")


if __name__ == "__main__":
    main()

