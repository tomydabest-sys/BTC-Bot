"""Phase 3.5 — reference (exogenous truth) weather signal.

Fetch the observable each weather market resolves on, so reaction-latency can be
measured. Resolution source is Wunderground (blocked); Open-Meteo at the same
station is used as a PROXY (see FINDINGS.md §5).

Granularity caveat: Open-Meteo's forecast endpoint serves HOURLY past data
(~90-day reach). Reaction latency measured against an hourly reference is a
COARSE proxy (≈1h resolution), NOT a sub-second latency budget — that would need
high-frequency station data we cannot retrieve historically. Confidence on
reaction features is therefore LOW–MEDIUM and labelled as such downstream.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..common.config import load_config
from ..common.db import connect, init_db
from ..common.http import get_json

log = logging.getLogger("recon.reference")

REF_SCHEMA = """
CREATE TABLE IF NOT EXISTS reference_temp (
    station TEXT, ts INTEGER, temp_f REAL,
    PRIMARY KEY (station, ts)
);
"""


def fetch_reference_series(station: str, start_date: str, end_date: str,
                          cache_bust: bool = False) -> list[tuple[int, float]]:
    """Hourly °F (UTC) for a station over [start_date, end_date] (YYYY-MM-DD)."""
    cfg = load_config()
    st = cfg["weather_reference"]["stations"].get(station)
    if not st:
        log.warning("no coordinates for station %s; skipping reference", station)
        return []
    url = cfg["weather_reference"]["open_meteo"]["forecast_url"]
    data = get_json(url, source="open_meteo", cache_bust=cache_bust, params={
        "latitude": st["lat"], "longitude": st["lon"],
        "hourly": "temperature_2m", "temperature_unit": "fahrenheit",
        "timezone": "GMT", "start_date": start_date, "end_date": end_date,
    })
    h = data.get("hourly", {})
    times, temps = h.get("time", []), h.get("temperature_2m", [])
    out: list[tuple[int, float]] = []
    for t, v in zip(times, temps):
        if v is None:
            continue
        ts = int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp())
        out.append((ts, float(v)))
    return out


def persist_reference(station: str, series: list[tuple[int, float]]) -> int:
    conn = connect()
    init_db(conn)
    conn.executescript(REF_SCHEMA)
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO reference_temp (station, ts, temp_f) VALUES (?,?,?)",
            [(station, ts, v) for ts, v in series])
    n = conn.execute("SELECT COUNT(*) c FROM reference_temp WHERE station=?",
                     (station,)).fetchone()["c"]
    conn.close()
    return n


def build_reference_for_window(cache_bust: bool = False) -> dict:
    """Fetch+persist reference series for every station present in `markets`."""
    conn = connect()
    init_db(conn)
    conn.executescript(REF_SCHEMA)
    rows = conn.execute(
        """SELECT station, MIN(date(start_date)) lo, MAX(date(end_date)) hi
           FROM markets WHERE station IS NOT NULL GROUP BY station""").fetchall()
    conn.close()
    out = {}
    for r in rows:
        series = fetch_reference_series(r["station"], r["lo"], r["hi"], cache_bust)
        out[r["station"]] = persist_reference(r["station"], series)
        log.info("reference %s: %d hourly points (%s..%s)", r["station"], out[r["station"]], r["lo"], r["hi"])
    return out
