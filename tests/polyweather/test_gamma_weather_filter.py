"""Strict weather-market filtering (GammaClient._is_weather_event).

The live scanner used a loose keyword substring filter, so "rain" inside
"Ukraine" (and "snow"/"temp" inside unrelated words) admitted NHL, geopolitics
and health markets — the operator saw `station_unresolved` spam for an NHL
game, a Russia/Ukraine ceasefire market and a WHO Hantavirus market. These
tests pin that only genuine city-temperature markets pass.
"""

from __future__ import annotations

from polybot.polyweather.exchanges.gamma_client import GammaClient, WeatherEvent


def _event(title: str = "", slug: str = "") -> WeatherEvent:
    return WeatherEvent(
        id="e", slug=slug, title=title, category="", tags=[], end_date="",
        volume_24hr=10_000.0, active=True, closed=False, rules="", buckets=[],
    )


# The three junk markets straight from the operator's terminal output.
JUNK = [
    _event(title="Canadiens vs Maple Leafs — who wins?", slug="nhl-mtl-tor-may-29"),
    _event(title="Russia x Ukraine ceasefire by May 31?", slug="russia-ukraine-ceasefire"),
    _event(title="Will the WHO characterize Hantavirus as a pandemic?", slug="who-hantavirus"),
]

# Real Polymarket weather markets (the /weather page), various title/slug shapes.
WEATHER = [
    _event(title="Highest temperature in London on May 29", slug="highest-temperature-london-may-29"),
    _event(title="Chicago high temperature Jun 15", slug="chicago-high-temp-jun-15"),
    _event(title="What will be the high temperature in NYC?", slug="nyc-high-temperature"),
    _event(title="Lowest temperature in Paris on May 30", slug="lowest-temperature-paris"),
    _event(title="", slug="highest-temperature-in-tokyo-on-may-29"),  # slug-only
]


def test_junk_markets_are_rejected() -> None:
    g = GammaClient()
    for ev in JUNK:
        assert not g._is_weather_event(ev), f"junk leaked: {ev.title!r} / {ev.slug!r}"


def test_real_temperature_markets_are_accepted() -> None:
    g = GammaClient()
    for ev in WEATHER:
        assert g._is_weather_event(ev), f"weather rejected: {ev.title!r} / {ev.slug!r}"


def test_ukraine_substring_no_longer_matches_rain() -> None:
    # The exact regression: "rain" is a substring of "Ukraine".
    g = GammaClient()
    assert not g._is_weather_event(_event(title="Russia and Ukraine peace deal?"))
    # And a precipitation market is also excluded — the bot only trades temps.
    assert not g._is_weather_event(_event(title="Will it rain in London tomorrow?"))
