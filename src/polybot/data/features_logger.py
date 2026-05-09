"""Feature row logger — writes per-cycle market state for offline analysis.

PATCHED: added cleanup_old_rows() for retention. Periodic call from main.py
prevents features.db from growing unbounded.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from polybot.data.models import MarketSnapshot, Order, Signal

logger = structlog.get_logger()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    ts_unix REAL NOT NULL,
    cycle_id TEXT,
    market_id TEXT,
    timeframe TEXT,
    strategy TEXT,
    direction TEXT,
    target_price REAL,
    confidence REAL,
    edge_bps REAL,
    fair_value REAL,
    mid REAL,
    best_bid REAL,
    best_ask REAL,
    spread_bps REAL,
    btc_move_5s REAL,
    btc_move_30s REAL,
    btc_move_60s REAL,
    poly_burst_5s REAL,
    time_to_expiry_s REAL,
    size_usd REAL,
    fill_price REAL,
    fill_size REAL,
    extra_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_features_ts_unix ON features (ts_unix);
CREATE INDEX IF NOT EXISTS idx_features_market_ts ON features (market_id, ts_unix);
CREATE INDEX IF NOT EXISTS idx_features_strategy ON features (strategy);
"""


_REQUIRED_FEATURES_COLUMNS: frozenset[str] = frozenset({
    "id", "ts", "ts_unix", "cycle_id", "market_id", "timeframe", "strategy",
    "direction", "target_price", "confidence", "edge_bps", "fair_value",
    "mid", "best_bid", "best_ask", "spread_bps",
    "btc_move_5s", "btc_move_30s", "btc_move_60s", "poly_burst_5s",
    "time_to_expiry_s", "size_usd", "fill_price", "fill_size", "extra_json",
})


def _migrate_legacy_features_table(conn: sqlite3.Connection) -> None:
    """Rename a pre-existing `features` table out of the way if its columns
    don't match the current schema.

    Without this, `CREATE TABLE IF NOT EXISTS features (...)` is a no-op on
    the stale table, and the subsequent `CREATE INDEX … ON features (ts_unix)`
    raises `sqlite3.OperationalError: no such column: ts_unix`. Renaming
    preserves the legacy rows under `features_legacy_<unix_ts>` for offline
    inspection while letting the fresh schema be created cleanly.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='features'"
    ).fetchone()
    if row is None:
        return

    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
    if _REQUIRED_FEATURES_COLUMNS.issubset(existing_cols):
        return  # already compatible

    # Drop legacy indexes first — some may reference columns we're about to
    # rename out from under them, and SQLite carries indexes with the table
    # rename which could collide on the new CREATE INDEX.
    legacy_indexes = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='features' "
        "AND name LIKE 'idx_features_%'"
    ).fetchall()
    for (idx_name,) in legacy_indexes:
        try:
            conn.execute(f"DROP INDEX IF EXISTS {idx_name}")
        except sqlite3.OperationalError:
            pass

    legacy_name = f"features_legacy_{int(time.time())}"
    conn.execute(f"ALTER TABLE features RENAME TO {legacy_name}")
    conn.commit()
    logger.warning(
        "features_db_migrated",
        legacy_table=legacy_name,
        missing_cols=sorted(_REQUIRED_FEATURES_COLUMNS - existing_cols),
        note="old rows preserved; schema rebuilt to match current code",
    )


class FeaturesLogger:
    """SQLite writer for per-cycle feature rows."""

    def __init__(self, db_path: str = "data/features.db") -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        _migrate_legacy_features_table(self._conn)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._writes_since_commit = 0

    def log_signal(
        self,
        snapshot: MarketSnapshot,
        signal: Signal,
        order: Order | None = None,
    ) -> None:
        """Write a single feature row from a signal + optional executed order."""
        try:
            ob = snapshot.orderbook
            now = datetime.now(timezone.utc)
            row = {
                "ts": now.isoformat(),
                "ts_unix": now.timestamp(),
                "cycle_id": "",
                "market_id": snapshot.market.id,
                "timeframe": "",
                "strategy": signal.strategy,
                "direction": signal.direction.value,
                "target_price": signal.target_price,
                "confidence": signal.confidence,
                "edge_bps": float((signal.metadata or {}).get("edge_bps", 0.0)),
                "fair_value": float((signal.metadata or {}).get("fair_value", 0.0)),
                "mid": ob.mid_price,
                "best_bid": ob.best_bid,
                "best_ask": ob.best_ask,
                "spread_bps": ob.spread * 10000,
                "btc_move_5s": float((signal.metadata or {}).get("btc_move_5s", 0.0)),
                "btc_move_30s": float((signal.metadata or {}).get("btc_move_30s", 0.0)),
                "btc_move_60s": float((signal.metadata or {}).get("btc_move_60s", 0.0)),
                "poly_burst_5s": float(getattr(snapshot, "poly_move_5s", 0.0) or 0.0),
                "time_to_expiry_s": float((signal.metadata or {}).get("t_rem", 0.0)),
                "size_usd": (order.size * order.price) if order else 0.0,
                "fill_price": order.avg_fill_price if order else 0.0,
                "fill_size": order.filled_size if order else 0.0,
                "extra_json": json.dumps(signal.metadata or {}, default=str),
            }
            self._conn.execute(
                """
                INSERT INTO features (
                    ts, ts_unix, cycle_id, market_id, timeframe, strategy,
                    direction, target_price, confidence, edge_bps, fair_value,
                    mid, best_bid, best_ask, spread_bps,
                    btc_move_5s, btc_move_30s, btc_move_60s, poly_burst_5s,
                    time_to_expiry_s, size_usd, fill_price, fill_size, extra_json
                ) VALUES (
                    :ts, :ts_unix, :cycle_id, :market_id, :timeframe, :strategy,
                    :direction, :target_price, :confidence, :edge_bps, :fair_value,
                    :mid, :best_bid, :best_ask, :spread_bps,
                    :btc_move_5s, :btc_move_30s, :btc_move_60s, :poly_burst_5s,
                    :time_to_expiry_s, :size_usd, :fill_price, :fill_size, :extra_json
                )
                """,
                row,
            )
            self._writes_since_commit += 1
            if self._writes_since_commit >= 25:
                self._conn.commit()
                self._writes_since_commit = 0
        except Exception as e:
            logger.warning("features_log_err", error=str(e))

    def cleanup_old_rows(self, keep_days: int = 30) -> int:
        """Delete rows older than `keep_days`. Returns number of rows deleted.

        Uses ts_unix for fast indexed deletion.
        """
        try:
            cutoff_dt = datetime.now(timezone.utc) - timedelta(days=keep_days)
            cutoff_unix = cutoff_dt.timestamp()
            cur = self._conn.execute(
                "DELETE FROM features WHERE ts_unix < ?",
                (cutoff_unix,),
            )
            deleted = cur.rowcount or 0
            self._conn.commit()
            if deleted > 0:
                # Run VACUUM after a large cleanup to reclaim disk space.
                # Only do this for big cleanups to avoid the lock cost.
                if deleted > 10000:
                    try:
                        self._conn.execute("VACUUM")
                    except sqlite3.OperationalError:
                        # VACUUM can't run inside a transaction; ignore if it fails
                        pass
            return deleted
        except Exception as e:
            logger.warning("features_cleanup_err", error=str(e))
            return 0

    def close(self) -> None:
        try:
            self._conn.commit()
            self._conn.close()
        except Exception:
            pass
