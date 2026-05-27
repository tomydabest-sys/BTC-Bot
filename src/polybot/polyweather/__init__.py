"""PolyWeather-Bot v1 — Polymarket weather-market trading bot.

Sits alongside the existing BTC-Bot code in the same package, reuses
``polybot.data.models`` (Signal/Direction/Market/etc.) and the proven
risk/orchestration scaffolding. Everything here is mock-clean by design:
``BOT_MOCK_DATA=true`` makes the bot runnable end-to-end with zero
credentials.
"""

from __future__ import annotations

__version__ = "1.0.0"

FIXTURE_ROOT_ENV = "POLYWEATHER_FIXTURE_ROOT"
