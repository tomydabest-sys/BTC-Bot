"""UK Met Office DataHub client + MockClient.

Free tier: 360 calls/day. Daily quota counter is persisted to SQLite.
"""

from __future__ import annotations

import os

import httpx

from polybot.polyweather._fixtures import load_fixture
from polybot.polyweather.data.forecasts.nws_client import ForecastPoint

DATAHUB = "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/hourly"


class MetOfficeClient:
    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("MET_OFFICE_API_KEY")
        if not key:
            raise RuntimeError("MET_OFFICE_API_KEY env var is required for live Met Office calls")
        self._headers = {"apikey": key, "accept": "application/json"}

    async def forecast(self, lat: float, lon: float) -> list[ForecastPoint]:
        url = f"{DATAHUB}?latitude={lat}&longitude={lon}"
        async with httpx.AsyncClient(timeout=10.0, headers=self._headers) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        out: list[ForecastPoint] = []
        for hour in data.get("features", []):
            props = hour.get("properties", {})
            t_c = props.get("screenTemperature")
            if t_c is None:
                continue
            out.append(
                ForecastPoint(
                    valid_time=props.get("time", ""),
                    horizon_hours=float(props.get("timeOffset", 0)),
                    predicted_temp_f=t_c * 9 / 5 + 32,
                    source="MetOffice",
                )
            )
        return out


class MockMetOfficeClient:
    def __init__(self) -> None:
        self._fixture = load_fixture("met_office_eglc.json")

    async def forecast(self, lat: float, lon: float) -> list[ForecastPoint]:
        return [
            ForecastPoint(
                valid_time=p["valid_time"],
                horizon_hours=float(p["horizon_hours"]),
                predicted_temp_f=float(p["predicted_temp_f"]),
                source="MetOffice",
            )
            for p in self._fixture["hourly"]
        ]
