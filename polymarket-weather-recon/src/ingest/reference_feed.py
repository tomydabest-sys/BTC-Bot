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
    source  TEXT,
    station TEXT,
    ts      INTEGER,
    temp_f  REAL,
    PRIMARY KEY (source, station, ts)
);
CREATE TABLE IF NOT EXISTS reference_validation (
    captured_at TEXT, station TEXT, target_date TEXT,
    source TEXT, ref_max_f REAL, ref_bucket TEXT,
    actual_winner TEXT, matched INTEGER,
    PRIMARY KEY (station, target_date, source)
);
"""


def _migrate_reference(conn) -> None:
    """If an older source-less reference_temp exists, rebuild it with a source
    column (tagging legacy rows 'open_meteo')."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reference_temp)").fetchall()}
    except Exception:
        return
    if cols and "source" not in cols:
        conn.executescript(
            "ALTER TABLE reference_temp RENAME TO reference_temp_old;"
            "CREATE TABLE reference_temp (source TEXT, station TEXT, ts INTEGER, temp_f REAL,"
            " PRIMARY KEY (source, station, ts));"
            "INSERT OR IGNORE INTO reference_temp SELECT 'open_meteo', station, ts, temp_f"
            " FROM reference_temp_old;"
            "DROP TABLE reference_temp_old;")
        conn.commit()


def _ensure_schema(conn) -> None:
    _migrate_reference(conn)
    conn.executescript(REF_SCHEMA)
    conn.commit()


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


def persist_reference(station: str, series: list[tuple[int, float]],
                      source: str = "open_meteo") -> int:
    conn = connect()
    init_db(conn)
    _ensure_schema(conn)
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO reference_temp (source, station, ts, temp_f) VALUES (?,?,?,?)",
            [(source, station, ts, v) for ts, v in series])
    n = conn.execute("SELECT COUNT(*) c FROM reference_temp WHERE station=? AND source=?",
                     (station, source)).fetchone()["c"]
    conn.close()
    return n


def build_reference_for_window(cache_bust: bool = False) -> dict:
    """Fetch+persist Open-Meteo reference series for every station in `markets`."""
    conn = connect()
    init_db(conn)
    _ensure_schema(conn)
    rows = conn.execute(
        """SELECT station, MIN(date(start_date)) lo, MAX(date(end_date)) hi
           FROM markets WHERE station IS NOT NULL GROUP BY station""").fetchall()
    conn.close()
    out = {}
    for r in rows:
        series = fetch_reference_series(r["station"], r["lo"], r["hi"], cache_bust)
        out[r["station"]] = persist_reference(r["station"], series, source="open_meteo")
        log.info("reference[open_meteo] %s: %d hourly points (%s..%s)",
                 r["station"], out[r["station"]], r["lo"], r["hi"])
    return out


# --------------------------------------------------------------------------- #
# NWS actual-observation reference (forward/live: ~2-day retention, 5-min METAR)
# --------------------------------------------------------------------------- #
def fetch_nws_series(station: str, cache_bust: bool = True) -> list[tuple[int, float]]:
    """Recent KLGA-style METAR observations -> (unix_ts, °F). NWS serves degC and
    only ~2 days of history (500-row cap), so this is a LIVE/forward feed, finer
    (5-min) than Open-Meteo hourly."""
    cfg = load_config()["weather_reference"]["nws"]
    data = get_json(f"{cfg['base_url']}/stations/{station}/observations",
                    source="nws", cache=False if cache_bust else True,
                    params={"limit": 500}, headers={"User-Agent": cfg["user_agent"]})
    out: list[tuple[int, float]] = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        t = (p.get("temperature") or {}).get("value")
        if t is None or not p.get("timestamp"):
            continue
        ts = int(datetime.fromisoformat(p["timestamp"]).timestamp())
        out.append((ts, t * 9 / 5 + 32))
    return out


def build_nws_reference(cache_bust: bool = True) -> dict:
    """Fetch+persist NWS observations for every station present in `markets`."""
    conn = connect()
    init_db(conn)
    _ensure_schema(conn)
    stations = [r["station"] for r in conn.execute(
        "SELECT DISTINCT station FROM markets WHERE station IS NOT NULL").fetchall()]
    conn.close()
    out = {}
    for st in stations:
        try:
            series = fetch_nws_series(st, cache_bust)
        except Exception as exc:
            log.warning("NWS fetch failed for %s: %s", st, exc)
            series = []
        out[st] = persist_reference(st, series, source="nws") if series else 0
        log.info("reference[nws] %s: %d obs", st, out[st])
    return out
