"""SQLite persistence layer for bot state."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import structlog

logger = structlog.get_logger()


class Storage:
    """Async SQLite storage for orders, positions, and P&L history."""

    def __init__(self, db_path: str = "./data/bot.db") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._create_tables()
        logger.info("storage_initialized", path=self._db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def _create_tables(self) -> None:
        assert self._db is not None
        await self._db.executescript("""
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

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                side TEXT NOT NULL,
                size REAL NOT NULL,
                avg_entry_price REAL NOT NULL,
                strategy TEXT,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                realized_pnl REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS pnl_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                realized_pnl REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                total_exposure REAL NOT NULL,
                num_positions INTEGER NOT NULL
            );
        """)
        await self._db.commit()

    async def save_order(self, order: dict) -> None:
        assert self._db is not None
        await self._db.execute(
            """INSERT OR REPLACE INTO orders
               (order_id, market_id, token_id, side, price, size, order_type,
                status, strategy, signal_id, filled_size, avg_fill_price,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order["order_id"],
                order["market_id"],
                order["token_id"],
                order["side"],
                order["price"],
                order["size"],
                order["order_type"],
                order["status"],
                order.get("strategy", ""),
                order.get("signal_id", ""),
                order.get("filled_size", 0),
                order.get("avg_fill_price", 0),
                order["created_at"],
                order["updated_at"],
            ),
        )
        await self._db.commit()

    async def save_pnl_snapshot(self, snapshot: dict) -> None:
        assert self._db is not None
        await self._db.execute(
            """INSERT INTO pnl_history
               (timestamp, realized_pnl, unrealized_pnl, total_exposure, num_positions)
               VALUES (?, ?, ?, ?, ?)""",
            (
                snapshot["timestamp"],
                snapshot["realized_pnl"],
                snapshot["unrealized_pnl"],
                snapshot["total_exposure"],
                snapshot["num_positions"],
            ),
        )
        await self._db.commit()
