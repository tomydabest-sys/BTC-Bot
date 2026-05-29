"""SQLite system-of-record (Phase 2+).

Schema is intentionally small and stable. Two domain tables plus an ingest log
for resumability. Idempotency comes from deterministic primary keys + INSERT OR
IGNORE, so every ingestion job is safe to re-run.

Important honesty flag baked into the schema: `trades.side_attribution` is
always 'taker_only' under the current network allowlist — the Data API does not
expose the maker counterparty (see FINDINGS.md §2). Downstream code must treat
per-wallet aggregates as taker-side.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import load_config, resolve_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id     TEXT PRIMARY KEY,
    event_id         TEXT,
    event_slug       TEXT,
    event_title      TEXT,
    question         TEXT,
    bucket_label     TEXT,
    city             TEXT,
    metric           TEXT,
    token_yes        TEXT,
    token_no         TEXT,
    resolution_source TEXT,
    station          TEXT,
    start_date       TEXT,
    end_date         TEXT,
    closed           INTEGER,
    neg_risk         INTEGER,
    resolved         INTEGER,        -- 1 if outcomePrices are 0/1
    winning_outcome_index INTEGER,   -- 0 = YES won, 1 = NO won, NULL = unresolved
    discovered_at    TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    trade_uid        TEXT PRIMARY KEY,
    condition_id     TEXT,
    asset            TEXT,           -- ERC-1155 token id (decimal string)
    proxy_wallet     TEXT,
    side             TEXT,           -- BUY / SELL (of the taker)
    side_attribution TEXT,          -- 'taker_only' (maker not available)
    size             REAL,          -- shares (human decimals)
    price            REAL,          -- USDC/share in [0,1]
    usdc             REAL,          -- size * price
    outcome          TEXT,
    outcome_index    INTEGER,
    timestamp        INTEGER,       -- unix seconds
    ts_iso           TEXT,
    transaction_hash TEXT,
    city             TEXT,
    question         TEXT,
    ingested_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_cond   ON trades(condition_id);
CREATE INDEX IF NOT EXISTS idx_trades_wallet ON trades(proxy_wallet);
CREATE INDEX IF NOT EXISTS idx_trades_ts     ON trades(timestamp);

CREATE TABLE IF NOT EXISTS ingest_log (
    condition_id  TEXT PRIMARY KEY,
    rows_ingested INTEGER,
    pages_fetched INTEGER,
    complete      INTEGER,
    fetched_at    TEXT
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    p = resolve_path(path or load_config()["storage"]["sqlite_path"])
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotently add columns introduced after a table already existed."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(markets)").fetchall()}
    for col, decl in (("resolved", "INTEGER"), ("winning_outcome_index", "INTEGER")):
        if col not in have:
            conn.execute(f"ALTER TABLE markets ADD COLUMN {col} {decl}")
