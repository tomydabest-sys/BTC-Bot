"""Phase 3.5 (real-feed) — IEM ASOS intraday observations.

Fetches REAL station temperature observations from the Iowa Environmental
Mesonet (IEM) ASOS archive and persists them source-tagged ``'iem'`` into
``reference_temp``, alongside the (unfit) Open-Meteo proxy.

Why this exists: the Open-Meteo model proxy carries a ~+1.4°F warm bias and only
lands in the 2°F winning bucket ~35% of the time (reports/backtest_p1.md), so it
cannot drive the P1 resolution-drift strategy. IEM serves the actual METAR-derived
station obs — the same observable Wunderground resolves these markets on — so it
is the right feed to test whether a station feed gives an information lead over
the market on resolution day.

IEM CSV shape (verified live):
    station,valid,tmpf
    LGA,2026-05-27 21:51,84.00
``tmpf`` is °F; ``valid`` is 'YYYY-MM-DD HH:MM' in UTC (we request tz=Etc/UTC).
US ASOS report ~hourly (:51), EU ~half-hourly; 5-min grid slots with no ob come
back as a missing token and are dropped. EU values are exact whole-°C
conversions (e.g. 68.00°F == 20.0°C), which is what the °C London/Paris buckets
need.

This is read-only recon: no BTC-Bot runtime is touched.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from ..common.config import load_config, project_root
from ..common.db import connect, init_db
from .reference_feed import _ensure_schema, persist_reference

log = logging.getLogger("recon.iem")

# Defaults; overridden by config.weather_reference.iem if present (config-driven).
IEM_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# Polymarket resolution-source station code -> IEM ASOS station id.
# US codes drop the leading 'K'; ICAO (EU) codes are used as-is. Paris markets
# resolve via LFPB (Le Bourget) in the live data, but LFPG is mapped too in case
# an event's resolutionSource points there.
IEM_STATION_MAP = {
    "KLGA": "LGA", "KORD": "ORD", "KMIA": "MIA",
    "EGLC": "EGLC", "LFPB": "LFPB", "LFPG": "LFPG",
}


def _iem_cfg() -> dict:
    return (load_config().get("weather_reference", {}) or {}).get("iem", {}) or {}


def _base_url() -> str:
    return _iem_cfg().get("base_url", IEM_BASE)


def _station_map() -> dict:
    return _iem_cfg().get("station_map") or IEM_STATION_MAP

# Tokens IEM uses for "no observation" (we also request missing=null).
_MISSING = {"M", "null", "", "T", "NA", "None"}


def _shift_date(d: str, days: int) -> str:
    return (datetime.strptime(d[:10], "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def _cache_path(iem_station: str, start_date: str, end_date: str):
    key = hashlib.sha256(f"{iem_station}|{start_date}|{end_date}".encode()).hexdigest()[:16]
    d = project_root() / "data" / "raw" / "iem"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{iem_station}_{start_date}_{end_date}_{key}.csv"


def fetch_iem_csv(iem_station: str, start_date: str, end_date: str,
                  cache_bust: bool = False) -> str:
    """Raw IEM ASOS CSV for [start_date, end_date) (YYYY-MM-DD; end exclusive,
    matching IEM's behaviour). Cached on disk; backs off on 429/5xx/network so
    re-runs are cheap and IEM's rate limit is respected."""
    cpath = _cache_path(iem_station, start_date, end_date)
    if cpath.exists() and not cache_bust:
        log.debug("cache hit IEM %s %s..%s", iem_station, start_date, end_date)
        return cpath.read_text()

    y1, m1, d1 = start_date[:10].split("-")
    y2, m2, d2 = end_date[:10].split("-")
    params = {
        "station": iem_station, "data": "tmpf",
        "year1": y1, "month1": m1, "day1": d1,
        "year2": y2, "month2": m2, "day2": d2,
        "tz": "Etc/UTC", "format": "onlycomma", "missing": "null", "latlon": "no",
    }
    cfg = load_config().get("ingestion", {}).get("http", {})
    timeout = cfg.get("timeout_seconds", 30)
    max_retries = cfg.get("max_retries", 4)
    backoff = cfg.get("backoff_base_seconds", 2)
    polite = cfg.get("polite_delay_seconds", 0.05)

    last: Exception | None = None
    base = _base_url()
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(base, params=params, timeout=timeout)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"retryable status {resp.status_code}")
            resp.raise_for_status()
            text = resp.text
            header = (text.splitlines() or [""])[0].lower()
            if "station" not in header or "tmpf" not in header:
                raise ValueError(f"unexpected IEM body (header={header!r:.80})")
            cpath.write_text(text)
            if polite:
                time.sleep(polite)
            return text
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if attempt >= max_retries:
                break
            wait = backoff * (2 ** attempt)
            log.warning("IEM %s failed (attempt %d/%d): %s; retrying in %ss",
                        iem_station, attempt + 1, max_retries, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"IEM fetch failed after retries: {iem_station} {start_date}..{end_date}: {last}")


def parse_iem_csv(text: str) -> list[tuple[int, float]]:
    """IEM CSV text -> sorted, de-duplicated [(unix_ts_utc, temp_f)] of REAL obs
    (missing/non-numeric rows dropped; repeated timestamps collapsed to last)."""
    dedup: dict[int, float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        v = (row.get("tmpf") or "").strip()
        valid = (row.get("valid") or "").strip()
        if not valid or v in _MISSING:
            continue
        try:
            tf = float(v)
        except ValueError:
            continue
        try:
            dt = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        dedup[int(dt.timestamp())] = tf
    return sorted(dedup.items())


def build_iem_reference_for_window(cache_bust: bool = False) -> dict:
    """Fetch + persist IEM ASOS obs for every station present in `markets`,
    over that station's market date span (widened ±2 days so local-day maxima at
    the window edges are complete across tz offsets). Persisted under the
    Polymarket station code so it joins to ``markets.station``; source='iem'."""
    conn = connect()
    init_db(conn)
    _ensure_schema(conn)
    rows = conn.execute(
        """SELECT station, MIN(date(start_date)) lo, MAX(date(end_date)) hi
           FROM markets WHERE station IS NOT NULL GROUP BY station""").fetchall()
    conn.close()

    station_map = _station_map()
    out: dict[str, int] = {}
    for r in rows:
        pm_station = r["station"]
        iem_id = station_map.get(pm_station)
        if not iem_id:
            log.warning("no IEM id for station %s; skipping", pm_station)
            out[pm_station] = 0
            continue
        start = _shift_date(r["lo"], -1)
        end = _shift_date(r["hi"], +2)          # IEM end is exclusive
        text = fetch_iem_csv(iem_id, start, end, cache_bust)
        series = parse_iem_csv(text)
        out[pm_station] = persist_reference(pm_station, series, source="iem") if series else 0
        log.info("reference[iem] %s (%s): %d obs (%s..%s)",
                 pm_station, iem_id, out[pm_station], start, end)
    return out
