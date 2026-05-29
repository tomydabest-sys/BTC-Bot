"""Resolution-date gate (GammaClient._is_past_resolution).

Polymarket leaves yesterday's daily-temperature markets listed as
``active=true&closed=false`` for hours after they resolve (UMA settlement
lag), and they sort to the *top* by 24h volume. Going online surfaced this:
the May-28 markets were the highest-volume "weather" events the morning of
May-29, with one bucket pinned at $0.999 and the rest at $0.001. The engine
would forecast a date in the past and the sub-15c override could "buy" an
already-decided bucket. These tests pin that resolved/past events are dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from polybot.polyweather.exchanges.gamma_client import GammaClient, WeatherEvent

NOW = datetime(2026, 5, 29, 2, 0, 0, tzinfo=UTC)


def _event(end_date: str = "", *, closed: bool = False) -> WeatherEvent:
    return WeatherEvent(
        id="e", slug="highest-temperature-in-london", title="Highest temperature in London",
        category="Weather", tags=["weather"], end_date=end_date,
        volume_24hr=100_000.0, active=True, closed=closed, rules="", buckets=[],
    )


def test_yesterdays_resolved_market_is_past() -> None:
    g = GammaClient()
    # The exact shape that leaked: end date noon the day before "now".
    assert g._is_past_resolution(_event("2026-05-28T12:00:00Z"), NOW)


def test_todays_live_market_is_not_past() -> None:
    g = GammaClient()
    # Today's market resolves at noon; at 02:00 it's still hours from settling.
    assert not g._is_past_resolution(_event("2026-05-29T12:00:00Z"), NOW)


def test_future_market_is_not_past() -> None:
    g = GammaClient()
    assert not g._is_past_resolution(_event("2026-05-30T12:00:00Z"), NOW)


def test_closed_flag_is_always_past() -> None:
    g = GammaClient()
    # Even with a future end date, an explicitly-closed event is resolved.
    assert g._is_past_resolution(_event("2026-06-30T12:00:00Z", closed=True), NOW)


def test_unparseable_end_date_is_not_dropped() -> None:
    # A date-format change must not silently drop every live market; let the
    # volume + bucket gates handle it instead.
    g = GammaClient()
    assert not g._is_past_resolution(_event(""), NOW)
    assert not g._is_past_resolution(_event("not-a-date"), NOW)


def test_grace_window_keeps_just_expired_market() -> None:
    # With a grace window, a market that expired seconds ago is still kept.
    g = GammaClient(resolution_grace_seconds=3600.0)
    just_past = (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert not g._is_past_resolution(_event(just_past), NOW)
    well_past = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert g._is_past_resolution(_event(well_past), NOW)
