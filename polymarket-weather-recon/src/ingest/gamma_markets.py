"""Phase 1 — Weather-market resolution.

Discover weather markets and map token ids -> human-readable markets, writing
reports/weather_markets.csv and persisting a `markets` table.

Scope is config-driven (ingestion.bounded_window): for the first pass this is
NYC temperature, last N days. Daily-temperature events have deterministic slugs
(verified live), so we enumerate by city x date — precise and cache-friendly —
rather than paginating the whole market universe.

Verified Phase-0 facts used here (see FINDINGS.md):
  * clobTokenIds/outcomes are stringified JSON arrays.
  * weather events are neg-risk multi-bucket (~11 YES/NO buckets per event).
  * resolutionSource is a Wunderground URL carrying the station code (KLGA).
"""
from __future__ import annotations

import csv
import logging
from datetime import date, datetime, timedelta, timezone

from ..common import normalize as N
from ..common.config import load_config, project_root
from ..common.db import connect, init_db
from ..common.http import get_json

log = logging.getLogger("recon.gamma")


def _window_dates(lookback_days: int, today: date | None = None) -> list[date]:
    today = today or datetime.now(timezone.utc).date()
    return [today - timedelta(days=i) for i in range(lookback_days, -1, -1)]


def _fetch_event(slug: str, base_url: str, cache_bust: bool) -> dict | None:
    data = get_json(f"{base_url}/events", source="gamma",
                    params={"slug": slug}, cache_bust=cache_bust)
    if isinstance(data, list) and data:
        return data[0]
    return None


def _markets_from_event(event: dict, city: str, metric: str) -> list[dict]:
    rows: list[dict] = []
    event_res = event.get("resolutionSource")
    for m in event.get("markets", []) or []:
        token_ids = N.parse_json_array(m.get("clobTokenIds"))
        res = m.get("resolutionSource") or event_res
        rows.append({
            "condition_id": m.get("conditionId"),
            "event_id": event.get("id"),
            "event_slug": event.get("slug"),
            "event_title": event.get("title"),
            "question": m.get("question"),
            "bucket_label": m.get("groupItemTitle") or m.get("question"),
            "city": city,
            "metric": metric,
            "token_yes": token_ids[0] if len(token_ids) > 0 else None,
            "token_no": token_ids[1] if len(token_ids) > 1 else None,
            "resolution_source": res,
            "station": N.station_from_resolution(res),
            "start_date": m.get("startDate") or event.get("startDate"),
            "end_date": m.get("endDate") or event.get("endDate"),
            "closed": 1 if m.get("closed") else 0,
            "neg_risk": 1 if (m.get("negRisk") or event.get("negRisk")) else 0,
        })
    return rows


def discover_weather_markets(cache_bust: bool = False, window: str = "bounded_window") -> list[dict]:
    cfg = load_config()
    base_url = cfg["sources"]["gamma"]["base_url"]
    disc = cfg["weather_discovery"]
    win = cfg["ingestion"][window]
    template = disc["event_slug_template"]
    city_slugs = disc["city_slugs"]
    metric = disc.get("metric", "temperature")
    dates = _window_dates(win["lookback_days"])

    discovered_at = datetime.now(timezone.utc).isoformat()
    all_rows: list[dict] = []
    missing: list[str] = []
    for city in win["cities"]:
        city_slug = city_slugs.get(city)
        if not city_slug:
            log.warning("no slug mapping for city %s — skipping", city)
            continue
        for d in dates:
            slug = N.build_event_slug(template, city_slug, d)
            event = _fetch_event(slug, base_url, cache_bust)
            if not event:
                missing.append(slug)
                continue
            rows = _markets_from_event(event, city, metric)
            all_rows.extend(rows)

    # ambiguity flag: any market whose station could not be parsed is surfaced
    # for manual review rather than silently kept/dropped.
    for r in all_rows:
        r["needs_review"] = r["station"] is None or r["condition_id"] is None

    _persist(all_rows, discovered_at)
    _write_csv(all_rows)
    log.info("discovered %d bucket-markets across %d events; %d slugs had no event",
             len(all_rows), len({r["event_slug"] for r in all_rows}), len(missing))
    return all_rows


def _persist(rows: list[dict], discovered_at: str) -> None:
    conn = connect()
    init_db(conn)
    with conn:
        for r in rows:
            conn.execute(
                """INSERT OR REPLACE INTO markets
                   (condition_id,event_id,event_slug,event_title,question,bucket_label,
                    city,metric,token_yes,token_no,resolution_source,station,
                    start_date,end_date,closed,neg_risk,discovered_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["condition_id"], r["event_id"], r["event_slug"], r["event_title"],
                 r["question"], r["bucket_label"], r["city"], r["metric"],
                 r["token_yes"], r["token_no"], r["resolution_source"], r["station"],
                 r["start_date"], r["end_date"], r["closed"], r["neg_risk"], discovered_at),
            )
    conn.close()


def _write_csv(rows: list[dict]) -> None:
    out = project_root() / "reports" / "weather_markets.csv"
    cols = ["condition_id", "event_slug", "event_title", "question", "bucket_label",
            "city", "metric", "token_yes", "token_no", "resolution_source", "station",
            "start_date", "end_date", "closed", "neg_risk", "needs_review"]
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
