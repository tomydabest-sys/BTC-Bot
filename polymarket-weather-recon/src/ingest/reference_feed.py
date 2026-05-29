"""Phase 3.5 — Reference (exogenous truth) weather signal (STUB).

Reconstruct the real-world observable each weather market resolves on and align
it to the trade stream, to compute per-wallet reaction-latency features.

Verified Phase-0 facts (see FINDINGS.md):
  * archive-api.open-meteo.com is BLOCKED, but the FORECAST endpoint
    (api.open-meteo.com/v1/forecast) returns past data via past_days (<=92).
    So ~90 days of historical hourly/daily series ARE reachable; the bounded
    first window is chosen to sit inside that range.
  * NWS (api.weather.gov) is reachable; observations come back in degC and
    recent-observation retention via the API is limited.
  * Resolution source observed = a Wunderground URL with an embedded station
    code (e.g. KLGA). Wunderground itself is blocked/scrape-only; Open-Meteo /
    NWS at the SAME station are a faithful proxy of the observable. Record this
    as a proxy (not the authoritative resolution value).
"""
from __future__ import annotations


def fetch_reference_series(station: str, start, end, cache_bust: bool = False):
    raise NotImplementedError("Phase 3.5 — pending Phase 0 checkpoint sign-off")
