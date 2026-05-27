"""NCEI 10-year base rates + MockClient.

Live calls hit ``https://www.ncei.noaa.gov/cdo-web/api/v2/data`` with the
NCEI token; cache 10 years of daily TMAX/TMIN/PRCP per station to local
SQLite (weekly refresh only). For v1 the live path is a stub — mock mode
returns deterministic, audit-friendly numbers.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import structlog

from polybot.polyweather._fixtures import load_fixture

logger = structlog.get_logger()


@dataclass
class BaseRateRow:
    station: str
    date: str
    bucket_low: float
    bucket_high: float
    base_rate: float
    sample_size: int


def _bucket_key(low: float, high: float) -> str:
    return f"{int(low)}_{int(high)}"


class NceiBaseRateClient:
    def __init__(
        self,
        token: str | None = None,
        cache_path: Path | str | None = None,
    ) -> None:
        self._token = token or os.environ.get("NCEI_TOKEN")
        if cache_path is None:
            # __file__ = .../src/polybot/polyweather/data/climatology/ncei_base_rates.py
            # parents[5] = repo root
            cache_path = (
                Path(__file__).resolve().parents[5]
                / "data" / "runtime" / "polyweather" / "ncei_cache.sqlite"
            )
        cache_path = Path(cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(cache_path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS base_rates ("
            "station TEXT, date TEXT, bucket_key TEXT, base_rate REAL, "
            "sample_size INTEGER, ts REAL, PRIMARY KEY (station, date, bucket_key))"
        )
        self._db.commit()

    def bucket_base_rate(
        self,
        station: str,
        date: str,
        temp_low: float,
        temp_high: float,
    ) -> float:
        key = _bucket_key(temp_low, temp_high)
        cur = self._db.execute(
            "SELECT base_rate FROM base_rates WHERE station=? AND date=? AND bucket_key=?",
            (station, date, key),
        )
        row = cur.fetchone()
        if row is not None:
            return float(row[0])
        # Live fetch not implemented in v1; return a uniform prior
        return 1.0 / 5  # 5 typical buckets → uniform

    def upsert(self, row: BaseRateRow) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO base_rates (station, date, bucket_key, base_rate, sample_size, ts) "
            "VALUES (?, ?, ?, ?, ?, strftime('%s', 'now'))",
            (row.station, row.date, _bucket_key(row.bucket_low, row.bucket_high),
             row.base_rate, row.sample_size),
        )
        self._db.commit()


class MockNceiBaseRateClient:
    def __init__(self) -> None:
        self._fixture = load_fixture("ncei_base_rates_klga.json")

    def bucket_base_rate(
        self,
        station: str,
        date: str,
        temp_low: float,
        temp_high: float,
    ) -> float:
        rates = self._fixture["base_rates_by_bucket_f"]
        key = _bucket_key(temp_low, temp_high)
        if key in rates:
            return float(rates[key])
        # If we don't have an exact match, return a neutral prior
        return 0.20
