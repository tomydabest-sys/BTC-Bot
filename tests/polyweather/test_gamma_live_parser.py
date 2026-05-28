"""Live-shape Gamma response parsing.

The fixtures use my hand-crafted schema (``token_id_yes``, ``bucket_low``,
etc.); real Polymarket Gamma responses use ``clobTokenIds`` JSON-strings,
``groupItemTitle`` for bucket labels, etc. These tests pin the parser's
behaviour on the real shape so future API drift is caught.
"""

from __future__ import annotations

from polybot.polyweather.exchanges.gamma_client import (
    _parse_bucket_label,
    _parse_event,
)


def test_parse_point_celsius_label() -> None:
    lo, hi, unit = _parse_bucket_label("26°C")
    assert (lo, hi, unit) == (26.0, 26.0, "C")


def test_parse_range_fahrenheit_label() -> None:
    lo, hi, unit = _parse_bucket_label("76-77°F")
    assert (lo, hi, unit) == (76.0, 77.0, "F")


def test_parse_or_higher_label() -> None:
    lo, hi, unit = _parse_bucket_label("34°C or higher")
    assert lo == 34.0
    assert hi == 999.0
    assert unit == "C"


def test_parse_under_label() -> None:
    lo, hi, unit = _parse_bucket_label("Under 70°F")
    assert lo == -999.0
    assert hi == 70.0
    assert unit == "F"


def test_parse_live_shaped_event() -> None:
    """A trimmed-down stand-in for the real Polymarket Gamma response."""
    raw = {
        "id": "evt-12345",
        "slug": "highest-temperature-london-may-29",
        "title": "Highest temperature in London on May 29",
        "description": "Resolves based on the maximum recorded temperature at London Heathrow (EGLL) as reported by the UK Met Office on May 29, 2026.",
        "endDate": "2026-05-30T00:00:00Z",
        "active": True,
        "closed": False,
        "volume24hr": 24000,
        "tags": [{"slug": "weather"}, {"slug": "temperature"}],
        "markets": [
            {
                "id": "mkt-26c",
                "conditionId": "0xabc...",
                "question": "Highest temperature in London on May 29: 26°C?",
                "groupItemTitle": "26°C",
                "clobTokenIds": "[\"123456789\", \"987654321\"]",
                "outcomes": "[\"Yes\", \"No\"]",
                "outcomePrices": "[\"0.34\", \"0.66\"]",
                "bestBid": 0.33,
                "bestAsk": 0.35,
                "volume24hrNum": 12000,
                "active": True,
                "closed": False,
            },
            {
                "id": "mkt-27c",
                "conditionId": "0xdef...",
                "question": "Highest temperature in London on May 29: 27°C?",
                "groupItemTitle": "27°C",
                "clobTokenIds": "[\"111111\", \"222222\"]",
                "outcomes": "[\"Yes\", \"No\"]",
                "outcomePrices": "[\"0.29\", \"0.71\"]",
                "bestBid": 0.28,
                "bestAsk": 0.30,
                "volume24hrNum": 8000,
                "active": True,
                "closed": False,
            },
        ],
    }
    event = _parse_event(raw)
    assert event.id == "evt-12345"
    assert event.title.startswith("Highest temperature in London")
    assert "weather" in event.tags
    assert event.volume_24hr == 24000
    assert len(event.buckets) == 2

    b26 = event.buckets[0]
    assert b26.id == "mkt-26c"
    assert b26.token_id_yes == "123456789"
    assert b26.token_id_no == "987654321"
    assert b26.bucket_low == 26.0
    assert b26.bucket_high == 26.0
    assert b26.unit == "C"
    assert 0.32 < b26.best_bid < 0.34
    assert 0.34 < b26.best_ask < 0.36

    b27 = event.buckets[1]
    assert b27.bucket_low == 27.0 and b27.bucket_high == 27.0


def test_parse_event_skips_closed_or_archived_markets() -> None:
    raw = {
        "id": "evt-x",
        "slug": "x",
        "title": "Test event",
        "endDate": "2026-06-01T00:00:00Z",
        "tags": [{"slug": "weather"}],
        "markets": [
            {
                "id": "open-one",
                "question": "Open?", "groupItemTitle": "25°C",
                "clobTokenIds": "[\"a\", \"b\"]",
                "outcomePrices": "[\"0.5\", \"0.5\"]",
                "active": True, "closed": False,
            },
            {
                "id": "closed-one",
                "question": "Closed?", "groupItemTitle": "26°C",
                "clobTokenIds": "[\"c\", \"d\"]",
                "outcomePrices": "[\"0.5\", \"0.5\"]",
                "closed": True,
            },
            {
                "id": "archived-one",
                "question": "Archived?", "groupItemTitle": "27°C",
                "clobTokenIds": "[\"e\", \"f\"]",
                "outcomePrices": "[\"0.5\", \"0.5\"]",
                "archived": True,
            },
        ],
    }
    event = _parse_event(raw)
    assert len(event.buckets) == 1
    assert event.buckets[0].id == "open-one"


def test_parse_event_skips_markets_without_clob_tokens() -> None:
    raw = {
        "id": "evt-y", "slug": "y", "title": "Test",
        "endDate": "2026-06-01T00:00:00Z",
        "tags": [{"slug": "temperature"}],
        "markets": [
            {"id": "no-tokens", "groupItemTitle": "30°C", "outcomePrices": "[\"0.5\", \"0.5\"]"},
        ],
    }
    event = _parse_event(raw)
    assert event.buckets == []
