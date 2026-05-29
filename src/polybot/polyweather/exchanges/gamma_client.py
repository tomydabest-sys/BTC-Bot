"""Polymarket Gamma API client (read-only market discovery) + MockClient.

Gamma is the marketplace metadata endpoint. We use it to enumerate active
weather events, their child bucket markets, and the resolution rules text
the station_resolver needs.

Live mode parses the real Polymarket response shape:

  Event
    ├── id, slug, title, endDate, description, tags
    └── markets[]                            (one per YES/NO question)
        ├── id, question, conditionId, slug
        ├── clobTokenIds   ("[<yes>, <no>]" JSON-string)
        ├── outcomes       ("[\"Yes\", \"No\"]")
        ├── outcomePrices  ("[\"0.34\", \"0.66\"]")
        ├── lastTradePrice / bestBid / bestAsk
        ├── volumeNum / volume24hrNum
        ├── groupItemTitle  (e.g. "26°C" — the bucket label)
        └── endDate, active, closed, archived

Buckets are parsed best-effort from ``groupItemTitle`` or ``question``
text; we accept Celsius and Fahrenheit, point markets ("26°C") and range
markets ("70-73°F"), and degrade gracefully (bucket_low == bucket_high
for point markets).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from polybot.polyweather._fixtures import load_fixture

logger = structlog.get_logger()

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Tag slugs to look for. Polymarket uses both "weather" and "temperature".
DEFAULT_WEATHER_TAG_SLUGS = ("weather", "temperature", "precipitation", "climate")

# A genuine Polymarket weather market (the https://polymarket.com/weather page)
# is a daily city high/low *temperature* market — e.g. "Highest temperature in
# London on May 29". We match that structure explicitly in the title or slug.
# The previous filter used loose keyword substrings ("rain", "snow", "temp"),
# which matched "rain" inside "Ukraine" and dragged in NHL, geopolitics and
# health markets. Precip/hurricane markets are intentionally excluded: the bot
# only models temperature buckets.
_TEMP_TITLE_RE = re.compile(
    r"\b(?:high(?:est)?|low(?:est)?|max(?:imum)?|min(?:imum)?)\s+temp(?:erature)?\b"
    r"|\btemperature\s+in\b",
    re.IGNORECASE,
)
_TEMP_SLUG_RE = re.compile(
    r"(?:high|highest|low|lowest|max|min)-temp(?:erature)?",
    re.IGNORECASE,
)


@dataclass
class WeatherBucket:
    """One bucket inside a weather event."""

    id: str
    token_id_yes: str
    token_id_no: str
    bucket_low: float
    bucket_high: float
    best_bid: float
    best_ask: float
    volume_24hr: float
    question: str = ""
    unit: str = "F"   # "F" or "C"


@dataclass
class WeatherEvent:
    """A weather event groups multiple bucket markets (e.g. high-temp ranges)."""

    id: str
    slug: str
    title: str
    category: str
    tags: list[str]
    end_date: str
    volume_24hr: float
    active: bool
    closed: bool
    rules: str
    buckets: list[WeatherBucket]


# ──────────────────────────────────────────────────────────────────────
#  Bucket-text parsing
# ──────────────────────────────────────────────────────────────────────

_RANGE_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*[-–to]+\s*(-?\d+(?:\.\d+)?)\s*[°]?\s*([FC])",
    re.IGNORECASE,
)
_POINT_OR_HIGHER_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*[°]?\s*([FC])\s*(?:or higher|or above|\+)",
    re.IGNORECASE,
)
_POINT_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*[°]?\s*([FC])\b",
    re.IGNORECASE,
)


def _parse_bucket_label(label: str) -> tuple[float, float, str]:
    """Extract (bucket_low, bucket_high, unit) from a Polymarket label.

    Examples
    --------
    "26°C"           → (26, 26, "C")
    "76-77°F"        → (76, 77, "F")
    "76°F or higher" → (76, 999, "F")
    "Under 70°F"     → (-999, 70, "F")     (heuristic)
    """
    if not label:
        return (-999.0, 999.0, "F")
    text = label.strip()

    # X-Y °unit
    m = _RANGE_RE.search(text)
    if m:
        lo, hi, unit = float(m.group(1)), float(m.group(2)), m.group(3).upper()
        if lo > hi:
            lo, hi = hi, lo
        return (lo, hi, unit)

    # X °unit or higher
    m = _POINT_OR_HIGHER_RE.search(text)
    if m:
        return (float(m.group(1)), 999.0, m.group(2).upper())

    # "Under X" / "below X"
    if re.search(r"\b(under|below|less than)\b", text, re.IGNORECASE):
        m = _POINT_RE.search(text)
        if m:
            return (-999.0, float(m.group(1)), m.group(2).upper())

    # Single point X °unit → treat as [X, X] (Polymarket point markets
    # resolve YES when the observed value equals X to the rounding step).
    m = _POINT_RE.search(text)
    if m:
        v = float(m.group(1))
        return (v, v, m.group(2).upper())

    return (-999.0, 999.0, "F")


def _parse_end_date(s: str) -> datetime | None:
    """Parse a Gamma ISO end-date into an aware UTC datetime, or None.

    Returns None when the field is empty or unparseable so callers can
    treat "unknown end date" differently from "definitely in the past".
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_json_list(s: Any) -> list:
    if isinstance(s, list):
        return s
    if not s:
        return []
    try:
        out = json.loads(s)
        if isinstance(out, list):
            return out
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return []


def _parse_market_node(raw: dict[str, Any]) -> WeatherBucket | None:
    """Convert one live Polymarket market into a ``WeatherBucket``."""
    # CLOB token ids — "[yes, no]" JSON string in live responses
    token_ids = _safe_json_list(raw.get("clobTokenIds"))
    if len(token_ids) < 2:
        return None
    token_yes, token_no = str(token_ids[0]), str(token_ids[1])

    # Prices — also stringified JSON
    prices = _safe_json_list(raw.get("outcomePrices") or raw.get("outcome_prices"))
    yes_price = None
    if len(prices) >= 1:
        try:
            yes_price = float(prices[0])
        except (TypeError, ValueError):
            yes_price = None

    # Best bid / ask — fall back to outcomePrices if individual fields missing
    best_bid = raw.get("bestBid") or raw.get("best_bid")
    best_ask = raw.get("bestAsk") or raw.get("best_ask")
    try:
        best_bid = float(best_bid) if best_bid is not None else (yes_price or 0.5)
    except (TypeError, ValueError):
        best_bid = yes_price or 0.5
    try:
        best_ask = float(best_ask) if best_ask is not None else (yes_price or 0.5)
    except (TypeError, ValueError):
        best_ask = yes_price or 0.5

    # Sanity-clamp prices
    best_bid = max(0.001, min(0.999, best_bid))
    best_ask = max(0.001, min(0.999, best_ask))
    if best_ask < best_bid:
        best_ask = best_bid

    label = raw.get("groupItemTitle") or raw.get("question") or ""
    bucket_low, bucket_high, unit = _parse_bucket_label(label)

    volume = raw.get("volume24hr") or raw.get("volume24hrNum") or raw.get("volumeNum") or 0.0
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        volume = 0.0

    market_id = str(raw.get("id") or raw.get("conditionId") or raw.get("slug") or "")
    if not market_id:
        return None

    return WeatherBucket(
        id=market_id,
        token_id_yes=token_yes,
        token_id_no=token_no,
        bucket_low=bucket_low,
        bucket_high=bucket_high,
        best_bid=best_bid,
        best_ask=best_ask,
        volume_24hr=volume,
        question=label,
        unit=unit,
    )


def _parse_event(raw: dict[str, Any]) -> WeatherEvent:
    """Parse a Polymarket Gamma event. Tolerant of missing fields."""
    # Mock-fixture compatibility: my fixtures use ``markets[].token_id_yes`` etc.
    if raw.get("markets") and raw["markets"] and "token_id_yes" in raw["markets"][0]:
        return _parse_fixture_event(raw)

    markets_raw = raw.get("markets", []) or []
    buckets: list[WeatherBucket] = []
    for m in markets_raw:
        if m.get("closed") or m.get("archived"):
            continue
        bucket = _parse_market_node(m)
        if bucket is not None:
            buckets.append(bucket)

    tags = raw.get("tags") or []
    tag_names: list[str] = []
    if isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict):
                slug = t.get("slug") or t.get("label")
                if slug:
                    tag_names.append(str(slug).lower())
            elif isinstance(t, str):
                tag_names.append(t.lower())

    return WeatherEvent(
        id=str(raw.get("id") or raw.get("slug") or ""),
        slug=str(raw.get("slug") or ""),
        title=str(raw.get("title") or ""),
        category=str(raw.get("category") or "Weather"),
        tags=tag_names,
        end_date=str(raw.get("endDate") or raw.get("end_date") or ""),
        volume_24hr=float(raw.get("volume24hr") or raw.get("volume_24hr") or 0.0),
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
        rules=str(raw.get("description") or raw.get("rules") or ""),
        buckets=buckets,
    )


def _parse_fixture_event(raw: dict[str, Any]) -> WeatherEvent:
    """The mock/test fixture format (predates the live schema work)."""
    buckets = [
        WeatherBucket(
            id=str(m["id"]),
            token_id_yes=m["token_id_yes"],
            token_id_no=m["token_id_no"],
            bucket_low=float(m["bucket_low"]),
            bucket_high=float(m["bucket_high"]),
            best_bid=float(m["best_bid"]),
            best_ask=float(m["best_ask"]),
            volume_24hr=float(m.get("volume_24hr", 0.0)),
            question=str(m.get("question", "")),
            unit="F",
        )
        for m in raw.get("markets", [])
    ]
    return WeatherEvent(
        id=raw["id"],
        slug=raw["slug"],
        title=raw["title"],
        category=raw.get("category", "Weather"),
        tags=list(raw.get("tags", [])),
        end_date=raw["end_date"],
        volume_24hr=float(raw.get("volume_24hr", 0.0)),
        active=bool(raw.get("active", True)),
        closed=bool(raw.get("closed", False)),
        rules=raw.get("rules", ""),
        buckets=buckets,
    )


class GammaClient:
    """Live Gamma client. Reads weather events from gamma-api.polymarket.com.

    Filtering pipeline:
      1. Query the weather *tag* directly (the /weather page), falling back to a
         broad active/open fetch if the tag query returns nothing.
      2. Strict local filter: title or slug must match a city temperature market
         (excludes NHL / geopolitics / health markets that the old substring
         keyword filter let through, e.g. "rain" inside "Ukraine").
      3. Resolution-date gate: skip events whose resolution time has already
         passed. Polymarket leaves yesterday's daily-temperature markets
         ``active=true&closed=false`` for hours after they resolve (UMA
         settlement lag), and they sort to the *top* by 24h volume. Without
         this gate the engine would forecast a date in the past and the
         sub-15c override could "buy" an already-decided bucket sitting at
         $0.001. Dropping them here also lets the engine's real-resolution
         check see an open position's market fall off the active list and
         settle it.
      4. Volume threshold.
      5. Skip events with no buckets we could parse.
    """

    def __init__(
        self,
        base_url: str = GAMMA_BASE,
        include_tag_slugs: tuple[str, ...] = DEFAULT_WEATHER_TAG_SLUGS,
        min_volume_24hr: float = 1000.0,
        timeout_s: float = 15.0,
        resolution_grace_seconds: float = 0.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._tag_slugs = {s.lower() for s in include_tag_slugs}
        # Primary tag used to query the /weather page directly.
        self._primary_tag = include_tag_slugs[0] if include_tag_slugs else "weather"
        self._min_volume = float(min_volume_24hr)
        self._timeout = timeout_s
        # An event is "past" once now > end_date + grace. Grace stays 0 by
        # default (resolution happens AT end_date) but is tunable for skew.
        self._resolution_grace_seconds = float(resolution_grace_seconds)

    async def list_active_weather_markets(self) -> list[WeatherEvent]:
        # Target the weather tag so we pull the /weather page rather than the
        # global top-of-book (where weather is a tiny, low-volume slice the
        # volume sort buries). If the tag query yields nothing (param drift or
        # empty result) fall back to a broad fetch — EITHER way the strict local
        # temperature filter below is the final guarantee.
        payload = await self._fetch_events(tag_slug=self._primary_tag)
        if not payload:
            payload = await self._fetch_events(tag_slug=None)

        now = datetime.now(timezone.utc)
        events: list[WeatherEvent] = []
        skipped_non_weather = 0
        skipped_resolved = 0
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            try:
                event = _parse_event(raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "gamma_event_parse_failed",
                    error=str(exc),
                    id=raw.get("id"),
                    title=str(raw.get("title", ""))[:60],
                )
                continue
            if not self._is_weather_event(event):
                skipped_non_weather += 1
                continue
            if self._is_past_resolution(event, now):
                skipped_resolved += 1
                continue
            if event.volume_24hr < self._min_volume:
                continue
            if not event.buckets:
                logger.info(
                    "gamma_event_no_buckets_parsed",
                    id=event.id,
                    title=event.title[:60],
                )
                continue
            events.append(event)

        logger.info(
            "gamma_weather_events_found",
            count=len(events),
            skipped_non_weather=skipped_non_weather,
            skipped_resolved=skipped_resolved,
        )
        return events

    async def _fetch_events(self, tag_slug: str | None) -> list[dict]:
        url = (
            f"{self._base}/events"
            "?active=true&closed=false&archived=false"
            "&order=volume24hr&ascending=false&limit=200"
        )
        if tag_slug:
            url += f"&tag_slug={tag_slug}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.error("gamma_fetch_failed", url=url, error=str(exc))
                return []

        # Gamma returns either a top-level list or {"events": [...]}.
        if isinstance(payload, dict):
            payload = payload.get("events") or payload.get("data") or []
        if not isinstance(payload, list):
            logger.error("gamma_unexpected_shape", type=str(type(payload)))
            return []
        return payload

    def _is_weather_event(self, event: WeatherEvent) -> bool:
        # Strict: must look like a daily city temperature market by title or
        # slug. This excludes "Ukraine" (substring "rain"), NHL, geopolitics,
        # health and every other non-weather market the old filter admitted.
        return bool(
            _TEMP_TITLE_RE.search(event.title or "")
            or _TEMP_SLUG_RE.search(event.slug or "")
        )

    def _is_past_resolution(self, event: WeatherEvent, now: datetime) -> bool:
        """True if the event has already resolved (or its end time has passed).

        ``closed`` events are obviously past. Otherwise compare the parsed
        end date against ``now`` (+ grace). An unparseable / missing end date
        is treated as NOT past — we'd rather rely on the volume and bucket
        gates than drop a live market on a date-format change.
        """
        if event.closed:
            return True
        end_dt = _parse_end_date(event.end_date)
        if end_dt is None:
            return False
        return now.timestamp() > (end_dt.timestamp() + self._resolution_grace_seconds)


class MockGammaClient:
    """Returns the 6-city fixture event list deterministically."""

    def __init__(self) -> None:
        self._fixture = load_fixture("gamma_active_weather_markets.json")

    async def list_active_weather_markets(self) -> list[WeatherEvent]:
        return [_parse_event(raw) for raw in self._fixture["events"]]
