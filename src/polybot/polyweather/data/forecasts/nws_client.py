"""NOAA NWS forecast client + MockClient.

``User-Agent`` is required by NWS — without it the endpoint returns 403.
On 403 (rate-limit signal): 5s exponential backoff, max 3 retries.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import httpx
import structlog

from polybot.polyweather._fixtures import load_fixture

logger = structlog.get_logger()


@dataclass
class ForecastPoint:
    """Single forecast period normalised across providers."""

    valid_time: str  # ISO8601 Z
    horizon_hours: float
    predicted_temp_f: float
    wind_mph: float | None = None
    precip_pct: float | None = None
    source: str = ""


class NwsClient:
    def __init__(self, user_agent_contact: str | None = None) -> None:
        contact = user_agent_contact or os.environ.get("NWS_USER_AGENT_CONTACT")
        if not contact:
            raise RuntimeError(
                "NWS_USER_AGENT_CONTACT env var is required for live NWS calls"
            )
        self._headers = {"User-Agent": f"PolyWeather-Bot ({contact})"}

    async def forecast(self, lat: float, lon: float) -> list[ForecastPoint]:
        url_points = f"https://api.weather.gov/points/{lat},{lon}"
        async with httpx.AsyncClient(timeout=10.0, headers=self._headers) as client:
            for attempt in range(3):
                resp = await client.get(url_points)
                if resp.status_code == 403:
                    await asyncio.sleep(5 * (2**attempt))
                    continue
                resp.raise_for_status()
                break
            else:
                raise RuntimeError("NWS rate-limited after 3 retries")
            forecast_url = resp.json()["properties"]["forecastHourly"]
            f = await client.get(forecast_url)
            f.raise_for_status()
            data = f.json()
        out: list[ForecastPoint] = []
        for period in data["properties"]["periods"]:
            out.append(
                ForecastPoint(
                    valid_time=period["startTime"],
                    horizon_hours=float(period.get("number", 1)),
                    predicted_temp_f=float(period["temperature"]),
                    source="NWS",
                )
            )
        return out


class MockNwsClient:
    def __init__(self) -> None:
        self._fixture = load_fixture("nws_forecast_klga_3day.json")

    async def forecast(self, lat: float, lon: float) -> list[ForecastPoint]:
        return [
            ForecastPoint(
                valid_time=p["valid_time"],
                horizon_hours=float(p["horizon_hours"]),
                predicted_temp_f=float(p["predicted_temp_f"]),
                wind_mph=float(p.get("wind_mph", 0.0)),
                precip_pct=float(p.get("precip_pct", 0.0)),
                source="NWS",
            )
            for p in self._fixture["periods"]
        ]
