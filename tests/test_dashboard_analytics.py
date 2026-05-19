"""Tests for the per-strategy analytics breakdown.

Covers the SESSION_HANDOFF observability gap: a profitable
overshoot_reversion entry/exit pair must show up under the right
strategy bucket in `compute_per_strategy_breakdown` (and therefore in
the `/api/analytics` payload's `per_strategy` key).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from polybot.dashboard.analytics import (
    _pair_trades,
    compute_per_strategy_breakdown,
    get_full_analytics,
)


def _orders_table(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE orders (
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
        """
    )


def _insert(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    market: str,
    side: str,
    price: float,
    size: float,
    strategy: str,
    created: datetime,
    filled_size: float | None = None,
    avg_fill: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO orders(order_id, market_id, token_id, side, price, size,
                           order_type, status, strategy, signal_id,
                           filled_size, avg_fill_price, created_at, updated_at)
        VALUES(?, ?, 'tok', ?, ?, ?, 'GTC', 'FILLED', ?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            market,
            side,
            price,
            size,
            strategy,
            f"sig-{order_id}",
            filled_size if filled_size is not None else size,
            avg_fill if avg_fill is not None else price,
            created.isoformat(),
            created.isoformat(),
        ),
    )


def test_profitable_overshoot_pair_shows_in_per_strategy(tmp_path):
    """A YES BUY @ 0.45 → SELL @ 0.50 entered by overshoot_reversion should
    surface under that strategy key with the correct P&L sign."""
    db = tmp_path / "bot.db"
    conn = sqlite3.connect(db)
    _orders_table(conn)
    t0 = datetime(2026, 5, 19, 12, 0, 0)
    _insert(
        conn,
        order_id="o-entry",
        market="m-1",
        side="BUY",
        price=0.45,
        size=10,
        strategy="overshoot_reversion",
        created=t0,
    )
    _insert(
        conn,
        order_id="o-exit",
        market="m-1",
        side="SELL",
        price=0.50,
        size=10,
        # Exit-side orders are tagged like `exit_<entry_strategy>` /
        # `auto_exit_<entry_strategy>` — _pair_trades flips the pair on
        # this prefix and preserves the ENTRY strategy on the TradePair.
        strategy="exit_overshoot_reversion",
        created=t0 + timedelta(seconds=30),
    )
    conn.commit()
    conn.close()

    payload = get_full_analytics(db_path=str(db))
    per_strategy = payload["per_strategy"]
    assert "overshoot_reversion" in per_strategy, (
        f"expected overshoot_reversion bucket; got {list(per_strategy)}"
    )
    bucket = per_strategy["overshoot_reversion"]
    assert bucket["total_trades"] == 1
    assert bucket["wins"] == 1
    assert bucket["losses"] == 0
    # P&L = (exit - entry) * size = (0.50 - 0.45) * 10 = 0.50
    assert bucket["total_pnl"] == pytest.approx(0.50, abs=1e-4)

    # And the exit strategy name must NOT show up as its own bucket —
    # the whole point of this view is to attribute by entry strategy.
    assert "exit_overshoot_reversion" not in per_strategy


def test_per_strategy_segregates_distinct_entry_strategies(tmp_path):
    db = tmp_path / "bot.db"
    conn = sqlite3.connect(db)
    _orders_table(conn)
    t0 = datetime(2026, 5, 19, 12, 0, 0)

    # overshoot_reversion: win
    _insert(conn, order_id="a1", market="m-1", side="BUY", price=0.40,
            size=10, strategy="overshoot_reversion", created=t0)
    _insert(conn, order_id="a2", market="m-1", side="SELL", price=0.50,
            size=10, strategy="exit_overshoot_reversion",
            created=t0 + timedelta(seconds=15))

    # boundary_decay: loss
    _insert(conn, order_id="b1", market="m-2", side="SELL", price=0.90,
            size=5, strategy="boundary_decay",
            created=t0 + timedelta(seconds=20))
    _insert(conn, order_id="b2", market="m-2", side="BUY", price=0.95,
            size=5, strategy="exit_boundary_decay",
            created=t0 + timedelta(seconds=40))

    conn.commit()
    conn.close()

    pairs = _pair_trades(
        [
            {
                "order_id": r[0], "market_id": r[1], "token_id": r[2],
                "side": r[3], "price": r[4], "size": r[5],
                "order_type": r[6], "status": r[7], "strategy": r[8],
                "signal_id": r[9], "filled_size": r[10],
                "avg_fill_price": r[11], "created_at": r[12],
                "updated_at": r[13],
            }
            for r in sqlite3.connect(db).execute("SELECT * FROM orders").fetchall()
        ]
    )
    per = compute_per_strategy_breakdown(pairs)
    assert set(per) == {"overshoot_reversion", "boundary_decay"}
    assert per["overshoot_reversion"]["wins"] == 1
    assert per["boundary_decay"]["losses"] == 1
    # boundary_decay sold @ 0.90 and bought back @ 0.95 → −$0.25
    assert per["boundary_decay"]["total_pnl"] == pytest.approx(-0.25, abs=1e-4)
