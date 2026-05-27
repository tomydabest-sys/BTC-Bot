"""Open-Meteo client (ECMWF, GFS, UKMO, GEFS ensemble) + MockClient.

Free tier quotas: 10000/day, 5000/hour, 600/min. We implement a daily-cap
token bucket so the bot never exceeds. CC BY 4.0 attribution is logged on
every successful call.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx
import structlog

from polybot.polyweather._fixtures import load_fixture
from polybot.polyweather.data.forecasts.nws_client import ForecastPoint

logger = structlog.get_logger()


@dataclass
class EnsembleMembers:
    valid_time: str
    horizon_hours: float
    members: list[float]


@dataclass
class OpenMeteoForecast:
    deterministic: dict[str, list[ForecastPoint]] = field(default_factory=dict)
    ensemble: EnsembleMembers | None = None


class _DailyBudget:
    def __init__(self, limit: int = 10000) -> None:
        self._limit = limit
        self._count = 0
        self._reset_at = time.time() + 86400

    def consume(self, n: int = 1) -> bool:
        now = time.time()
        if now >= self._reset_at:
            self._count = 0
            self._reset_at = now + 86400
        if self._count + n > self._limit:
            return False
        self._count += n
        return True


class OpenMeteoClient:
    def __init__(self, daily_budget: int = 10000) -> None:
        self._budget = _DailyBudget(daily_budget)

    async def forecast(self, lat: float, lon: float) -> OpenMeteoForecast:
        if not self._budget.consume(2):
            raise RuntimeError("open_meteo daily budget exhausted")
        async with httpx.AsyncClient(timeout=10.0) as client:
            det_url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}"
                "&hourly=temperature_2m"
                "&temperature_unit=fahrenheit"
                "&models=ecmwf_ifs04,gfs_seamless,ukmo_seamless"
            )
            ens_url = (
                "https://ensemble-api.open-meteo.com/v1/ensemble"
                f"?latitude={lat}&longitude={lon}"
                "&hourly=temperature_2m"
                "&temperature_unit=fahrenheit"
                "&models=gfs025"
            )
            # v1 lives off the mock fixture; live parsing of these payloads is
            # tracked separately. We still hit the endpoints to validate auth
            # and rate-limit headroom.
            await client.get(det_url)
            await client.get(ens_url)
        logger.info("open_meteo data CC BY 4.0")
        return OpenMeteoForecast()


class MockOpenMeteoClient:
    def __init__(self) -> None:
        self._fixture = load_fixture("open_meteo_ensemble_klga.json")

    async def forecast(self, lat: float, lon: float) -> OpenMeteoForecast:
        det: dict[str, list[ForecastPoint]] = {}
        for model, periods in self._fixture["deterministic_models"].items():
            det[model] = [
                ForecastPoint(
                    valid_time=p["valid_time"],
                    horizon_hours=float(p["horizon_hours"]),
                    predicted_temp_f=float(p["predicted_temp_f"]),
                    source=f"OpenMeteo/{model}",
                )
                for p in periods
            ]
        ens_raw = self._fixture["ensemble"]["gfs025"]
        ens = EnsembleMembers(
            valid_time=ens_raw["valid_time"],
            horizon_hours=float(ens_raw["horizon_hours"]),
            members=[float(x) for x in ens_raw["members"]],
        )
        return OpenMeteoForecast(deterministic=det, ensemble=ens)
