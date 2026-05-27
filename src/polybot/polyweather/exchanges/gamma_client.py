"""Polymarket Gamma API client (read-only market discovery) + MockClient.

Gamma is the marketplace metadata endpoint. We use it to enumerate active
weather events, their child bucket markets, and the resolution rules text
the station_resolver needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from polybot.polyweather._fixtures import load_fixture

logger = structlog.get_logger()

GAMMA_BASE = "https://gamma-api.polymarket.com"


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


def _parse_event(raw: dict[str, Any]) -> WeatherEvent:
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
    """Live Gamma client. Reads weather events that match include_tags."""

    def __init__(
        self,
        base_url: str = GAMMA_BASE,
        include_tags: list[str] | None = None,
        min_volume_24hr: float = 5000.0,
        timeout_s: float = 10.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._tags = set(include_tags or ["temperature", "precipitation"])
        self._min_volume = float(min_volume_24hr)
        self._timeout = timeout_s

    async def list_active_weather_markets(self) -> list[WeatherEvent]:
        url = (
            f"{self._base}/events?active=true&closed=false"
            f"&order=volume24hr&ascending=false&limit=100"
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
        events: list[WeatherEvent] = []
        for raw in payload.get("events", payload):
            tags = set(raw.get("tags", []))
            if not (tags & self._tags):
                continue
            if float(raw.get("volume_24hr", 0.0)) < self._min_volume:
                continue
            try:
                events.append(_parse_event(raw))
            except (KeyError, ValueError) as exc:
                logger.warning("gamma_event_parse_failed", error=str(exc), id=raw.get("id"))
        return events


class MockGammaClient:
    """Returns the 6-city fixture event list deterministically."""

    def __init__(self) -> None:
        self._fixture = load_fixture("gamma_active_weather_markets.json")

    async def list_active_weather_markets(self) -> list[WeatherEvent]:
        return [_parse_event(raw) for raw in self._fixture["events"]]
