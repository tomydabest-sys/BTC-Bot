"""Async SQLite feature logger — persists every trading-loop snapshot for offline analysis.

Writes to ./data/features.db. Batches inserts to avoid per-cycle fsync cost.
Hook: call log_snapshot() from the main trading loop once per market per cycle.
Backfill: call backfill_settlement() once resolution is known, to label rows.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
    ts              INTEGER NOT NULL,
    market_id       TEXT    NOT NULL,
    time_remaining  REAL,
    poly_mid        REAL,
    poly_spread     REAL,
    poly_bid_depth  REAL,
    poly_ask_depth  REAL,
    poly_trade_imb  REAL,
    btc_price       REAL,
    btc_move_5s     REAL,
    btc_move_30s    REAL,
    btc_move_300s   REAL,
    btc_vol_60s     REAL,
    poly_move_5s    REAL,
    poly_move_30s   REAL,
    settled_up      INTEGER,
    poly_mid_exp    REAL
);
CREATE INDEX IF NOT EXISTS idx_feat_market_ts ON features(market_id, ts);
CREATE INDEX IF NOT EXISTS idx_feat_ts ON features(ts);
CREATE INDEX IF NOT EXISTS idx_feat_unsettled ON features(settled_up) WHERE settled_up IS NULL;
"""


_INSERT_SQL = (
    "INSERT INTO features "
    "(ts, market_id, time_remaining, poly_mid, poly_spread, poly_bid_depth, poly_ask_depth, "
    "poly_trade_imb, btc_price, btc_move_5s, btc_move_30s, btc_move_300s, btc_vol_60s, "
    "poly_move_5s, poly_move_30s, settled_up, poly_mid_exp) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class FeaturesLogger:
    """Append-only feature store for every trading cycle snapshot.

    Usage:
        logger = FeaturesLogger("./data/features.db")
        await logger.start()
        ...
        await logger.log_snapshot(market_id, snapshot, btc_feed, time_remaining)
        ...
        await logger.stop()
    """

    def __init__(
        self,
        db_path: str = "./data/features.db",
        flush_interval_seconds: float = 5.0,
        max_buffer_rows: int = 200,
    ) -> None:
        self._db_path = db_path
        self._flush_interval = flush_interval_seconds
        self._max_buffer = max_buffer_rows
        self._buffer: list[tuple] = []
        self._db: aiosqlite.Connection | None = None
        self._flush_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._running = False
        self._rows_written = 0

    @property
    def rows_written(self) -> int:
        return self._rows_written

    async def start(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        self._running = True
        self._flush_task = asyncio.create_task(self._periodic_flush())
        logger.info("features_logger_started", path=self._db_path)

    async def stop(self) -> None:
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self._flush()
        except Exception as e:
            logger.error("features_final_flush_err", error=str(e))
        if self._db:
            await self._db.close()
            self._db = None
        logger.info("features_logger_stopped", rows_written=self._rows_written)

    async def log_snapshot(
        self,
        market_id: str,
        snapshot: Any,
        btc_feed: Any | None,
        time_remaining: float,
    ) -> None:
        """Log one feature row for a market.

        Safe to call from anywhere; buffers internally and flushes in background.
        Never raises — errors are swallowed and logged.
        """
        try:
            row = self._build_row(market_id, snapshot, btc_feed, time_remaining)
        except Exception as e:
            logger.debug("features_build_err", market_id=market_id[:16], error=str(e))
            return

        async with self._lock:
            self._buffer.append(row)
            over = len(self._buffer) >= self._max_buffer
        if over:
            try:
                await self._flush()
            except Exception as e:
                logger.warning("features_flush_threshold_err", error=str(e))

    async def backfill_settlement(
        self,
        market_id: str,
        settled_up: int,
        poly_mid_exp: float | None = None,
    ) -> int:
        """Mark all unsettled rows for this market with the final outcome.

        Returns number of rows updated.
        """
        if self._db is None:
            return 0
        try:
            cursor = await self._db.execute(
                "UPDATE features SET settled_up = ?, "
                "poly_mid_exp = COALESCE(?, poly_mid_exp) "
                "WHERE market_id = ? AND settled_up IS NULL",
                (int(settled_up), poly_mid_exp, market_id),
            )
            await self._db.commit()
            updated = cursor.rowcount or 0
            if updated > 0:
                logger.info(
                    "features_backfilled",
                    market_id=market_id[:16],
                    settled_up=settled_up,
                    rows=updated,
                )
            return updated
        except Exception as e:
            logger.error("features_backfill_err", market_id=market_id[:16], error=str(e))
            return 0

    async def count_unsettled(self) -> int:
        if self._db is None:
            return 0
        try:
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM features WHERE settled_up IS NULL"
            )
            row = await cursor.fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    # ──────────────────────────────────────────────────────────────────
    #  Internals
    # ──────────────────────────────────────────────────────────────────

    def _build_row(
        self,
        market_id: str,
        snapshot: Any,
        btc_feed: Any | None,
        time_remaining: float,
    ) -> tuple:
        ob = snapshot.orderbook
        poly_mid = float(ob.mid_price)
        poly_spread = float(ob.spread)
        bid_depth = float(ob.bid_depth)
        ask_depth = float(ob.ask_depth)

        # Trade imbalance over recent_trades (last 30s)
        buy_v = 0.0
        sell_v = 0.0
        now_dt = datetime.utcnow()
        for t in snapshot.recent_trades or []:
            try:
                ts = t.timestamp
                if hasattr(ts, "replace") and getattr(ts, "tzinfo", None) is not None:
                    ts = ts.replace(tzinfo=None)
                age = (now_dt - ts).total_seconds()
            except Exception:
                continue
            if age < 0 or age > 30:
                continue
            side_val = getattr(t.side, "value", str(t.side))
            if side_val == "BUY":
                buy_v += float(t.size)
            else:
                sell_v += float(t.size)
        total_v = buy_v + sell_v
        poly_trade_imb = ((buy_v - sell_v) / total_v) if total_v > 0 else 0.0

        # BTC features
        btc_price = 0.0
        btc_move_5s = 0.0
        btc_move_30s = 0.0
        btc_move_300s = 0.0
        btc_vol_60s = 0.0
        if btc_feed is not None and getattr(btc_feed, "last_price", 0) > 0:
            btc_price = float(btc_feed.last_price)
            btc_move_5s = float(btc_feed.price_change_since(5.0))
            btc_move_30s = float(btc_feed.price_change_since(30.0))
            elapsed_in_window = max(1.0, 300.0 - max(0.0, float(time_remaining)))
            btc_move_300s = float(
                btc_feed.price_change_since(min(300.0, elapsed_in_window))
            )
            btc_vol_60s = float(btc_feed.volatility_window(60))

        poly_move_5s = float(getattr(snapshot, "poly_move_5s", 0.0) or 0.0)
        poly_move_30s = float(getattr(snapshot, "poly_move_30s", 0.0) or 0.0)

        ts_ms = int(time.time() * 1000)

        return (
            ts_ms,
            market_id,
            float(time_remaining),
            poly_mid,
            poly_spread,
            bid_depth,
            ask_depth,
            poly_trade_imb,
            btc_price,
            btc_move_5s,
            btc_move_30s,
            btc_move_300s,
            btc_vol_60s,
            poly_move_5s,
            poly_move_30s,
            None,
            None,
        )

    async def _periodic_flush(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("features_periodic_flush_err", error=str(e))

    async def _flush(self) -> None:
        if self._db is None:
            return
        async with self._lock:
            if not self._buffer:
                return
            rows = self._buffer
            self._buffer = []
        try:
            await self._db.executemany(_INSERT_SQL, rows)
            await self._db.commit()
            self._rows_written += len(rows)
        except Exception as e:
            logger.error("features_insert_err", error=str(e), batch_size=len(rows))
