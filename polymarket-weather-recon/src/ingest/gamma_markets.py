"""Phase 1 — Weather-market resolution (STUB; implement after Phase 0 sign-off).

Resolve which Polymarket markets are weather markets and map token ids ->
human-readable markets, writing reports/weather_markets.csv.

Verified Phase-0 facts this will rely on (see FINDINGS.md):
  * Gamma /markets and /events cap at 100 rows/request -> paginate by offset.
  * clobTokenIds / outcomes / outcomePrices are STRINGIFIED JSON arrays
    (json.loads each before use).
  * `tag_slug` market filter is ignored -> discover via /public-search?q=<kw>
    (caps ~50/type) AND by paginating /events and filtering client-side on the
    `tags` array (labels: Weather / temperature / Daily Temperature) + keywords.
  * Weather events are neg-risk multi-bucket (one event -> ~11 YES/NO bucket
    markets); capture event + per-bucket condition ids and token ids.
  * resolutionSource is typically a Wunderground URL with an embedded station
    code (e.g. .../KLGA) -> parse out the station for Phase 3.5.
"""
from __future__ import annotations


def discover_weather_markets(cache_bust: bool = False):
    raise NotImplementedError("Phase 1 — pending Phase 0 checkpoint sign-off")
