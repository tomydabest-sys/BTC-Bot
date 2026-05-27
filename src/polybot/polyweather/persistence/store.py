"""SQLite-backed state for paper/live mode.

Separate files for paper and live so paper trade history can never
contaminate validation gate inputs for live (forbidden-list rule #10).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any

from polybot.polyweather.risk.validation_gate import TradePair

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    station TEXT NOT NULL,
    city TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    exit_price TEXT NOT NULL,
    size TEXT NOT NULL,
    fees_usdc TEXT NOT NULL,
    rebates_usdc TEXT NOT NULL,
    realised_pnl_usdc TEXT NOT NULL,
    opened_at REAL NOT NULL,
    closed_at REAL NOT NULL,
    fill_latency_seconds REAL NOT NULL,
    model_probability REAL NOT NULL,
    realised_outcome INTEGER NOT NULL,
    used_dynamic_fee INTEGER NOT NULL DEFAULT 1,
    cap_violation INTEGER NOT NULL DEFAULT 0,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_closed_at ON trades(closed_at);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
CREATE INDEX IF NOT EXISTS idx_trades_city ON trades(city);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    cycle_id TEXT,
    strategy TEXT,
    market_id TEXT,
    station TEXT,
    city TEXT,
    decision TEXT,
    reason TEXT,
    mid REAL,
    confidence REAL,
    edge_bps REAL,
    model_probability REAL,
    bucket_low REAL,
    bucket_high REAL,
    forecast_horizon_hours REAL,
    extra TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);

CREATE TABLE IF NOT EXISTS equity (
    ts REAL PRIMARY KEY,
    bankroll_usdc TEXT NOT NULL,
    daily_pnl_usdc TEXT NOT NULL,
    open_exposure_usdc TEXT NOT NULL
);
"""


def _to_dec(s: str) -> Decimal:
    return Decimal(str(s))


class PolyWeatherStore:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self._path)) as db:
            db.executescript(SCHEMA)
            db.commit()

    @property
    def path(self) -> Path:
        return self._path

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path)
        db.row_factory = sqlite3.Row
        return db

    # ─── decisions ───────────────────────────────────────────────────

    def record_decision(
        self,
        *,
        cycle_id: str,
        strategy: str,
        market_id: str,
        decision: str,
        reason: str,
        station: str | None = None,
        city: str | None = None,
        mid: float | None = None,
        confidence: float | None = None,
        edge_bps: float | None = None,
        model_probability: float | None = None,
        bucket_low: float | None = None,
        bucket_high: float | None = None,
        forecast_horizon_hours: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        with closing(self._connect()) as db:
            db.execute(
                "INSERT INTO decisions (ts, cycle_id, strategy, market_id, station, city, "
                "decision, reason, mid, confidence, edge_bps, model_probability, "
                "bucket_low, bucket_high, forecast_horizon_hours, extra) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(), cycle_id, strategy, market_id, station, city, decision, reason,
                    mid, confidence, edge_bps, model_probability, bucket_low, bucket_high,
                    forecast_horizon_hours,
                    json.dumps(extra) if extra is not None else None,
                ),
            )
            db.commit()

    def decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── trades ──────────────────────────────────────────────────────

    def record_trade(self, t: TradePair) -> int:
        with closing(self._connect()) as db:
            cur = db.execute(
                "INSERT INTO trades ("
                " market_id, event_id, strategy, station, city, side, entry_price, exit_price,"
                " size, fees_usdc, rebates_usdc, realised_pnl_usdc, opened_at, closed_at,"
                " fill_latency_seconds, model_probability, realised_outcome, used_dynamic_fee,"
                " cap_violation, metadata"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    t.market_id, t.event_id, t.strategy, t.station, t.city, t.side,
                    str(t.entry_price), str(t.exit_price), str(t.size),
                    str(t.fees_usdc), str(t.rebates_usdc), str(t.realised_pnl_usdc),
                    t.opened_at, t.closed_at, t.fill_latency_seconds,
                    t.model_probability, int(t.realised_outcome),
                    int(t.used_dynamic_fee), int(t.cap_violation),
                    json.dumps(t.metadata),
                ),
            )
            db.commit()
            return int(cur.lastrowid or 0)

    def trades(
        self,
        limit: int = 100,
        strategy: str | None = None,
        city: str | None = None,
    ) -> list[TradePair]:
        sql = "SELECT * FROM trades"
        params: list[Any] = []
        clauses: list[str] = []
        if strategy:
            clauses.append("strategy = ?")
            params.append(strategy)
        if city:
            clauses.append("city = ?")
            params.append(city)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY closed_at DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as db:
            rows = db.execute(sql, params).fetchall()
        return [_row_to_trade(r) for r in rows]

    def trade_count(self) -> int:
        with closing(self._connect()) as db:
            return int(db.execute("SELECT COUNT(*) FROM trades").fetchone()[0])

    def first_trade_ts(self) -> float | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT MIN(opened_at) FROM trades").fetchone()
            return float(row[0]) if row and row[0] is not None else None

    # ─── equity ──────────────────────────────────────────────────────

    def record_equity(
        self,
        bankroll: Decimal,
        daily_pnl: Decimal,
        open_exposure: Decimal,
    ) -> None:
        with closing(self._connect()) as db:
            db.execute(
                "INSERT OR REPLACE INTO equity (ts, bankroll_usdc, daily_pnl_usdc, open_exposure_usdc) "
                "VALUES (?, ?, ?, ?)",
                (time.time(), str(bankroll), str(daily_pnl), str(open_exposure)),
            )
            db.commit()

    def equity_history(self, limit: int = 1000) -> list[tuple[float, Decimal]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT ts, bankroll_usdc FROM equity ORDER BY ts ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(float(r["ts"]), _to_dec(r["bankroll_usdc"])) for r in rows]


def _row_to_trade(row: sqlite3.Row) -> TradePair:
    return TradePair(
        market_id=row["market_id"],
        event_id=row["event_id"],
        strategy=row["strategy"],
        station=row["station"],
        city=row["city"],
        side=row["side"],
        entry_price=_to_dec(row["entry_price"]),
        exit_price=_to_dec(row["exit_price"]),
        size=_to_dec(row["size"]),
        fees_usdc=_to_dec(row["fees_usdc"]),
        rebates_usdc=_to_dec(row["rebates_usdc"]),
        realised_pnl_usdc=_to_dec(row["realised_pnl_usdc"]),
        opened_at=float(row["opened_at"]),
        closed_at=float(row["closed_at"]),
        fill_latency_seconds=float(row["fill_latency_seconds"]),
        model_probability=float(row["model_probability"]),
        realised_outcome=int(row["realised_outcome"]),
        used_dynamic_fee=bool(row["used_dynamic_fee"]),
        cap_violation=bool(row["cap_violation"]),
        metadata=json.loads(row["metadata"]) if row["metadata"] else {},
    )


# Helper so dashboard can serialise Decimal cleanly to JSON
def decimal_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return f"{obj:.4f}"
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serialisable")
