"""DataApiClient parses the REAL data-api shape (bare list, camelCase).

The earlier stub assumed ``{"positions": [...]}`` with ``tokenId`` /
``marketId`` / ``currentPrice`` and parsed nothing against the live API.
These tests pin the real shape — a bare JSON list with ``asset`` /
``conditionId`` / ``avgPrice`` / ``curPrice`` / ``usdcSize`` — using records
captured from live wallets, and pin the weather classifier (a BTC up/down
record must NOT count as weather).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from polybot.polyweather._fixtures import load_fixture
from polybot.polyweather.exchanges.data_api_client import (
    _parse_activity,
    _parse_position,
    is_weather_market,
)

FIX = load_fixture("data_api_wallet_samples.json")


def test_position_parses_real_camelcase_shape() -> None:
    p = _parse_position(FIX["positions"][0])
    assert p is not None
    assert p.asset.startswith("16040300960242292466")
    assert p.condition_id.startswith("0x6797ea3")
    assert p.outcome == "No"
    assert p.size == Decimal("220.51")
    assert p.avg_price == Decimal("0.984")
    assert p.current_price == Decimal("0.983")     # from curPrice, not currentPrice
    assert p.cash_pnl == Decimal("-0.2359")
    assert p.realized_pnl == Decimal("12.5")
    assert p.is_weather is True


def test_activity_parses_trade_record() -> None:
    a = _parse_activity(FIX["activity"][0])
    assert a is not None
    assert a.type == "TRADE"
    assert a.side == "BUY"
    assert a.price == Decimal("0.983")
    assert a.usdc_size == Decimal("9.81867")
    assert a.timestamp == 1780020380
    assert a.is_weather is True


def test_btc_updown_is_not_weather() -> None:
    # The non-weather guard: a BTC up/down market must be rejected by the
    # same regex the scanner uses, so wallet-watch never reports crypto noise.
    pos = _parse_position(FIX["positions"][1])
    assert pos is not None and pos.is_weather is False
    act = _parse_activity(FIX["activity"][1])
    assert act is not None and act.is_weather is False


def test_is_weather_market_matches_title_or_slug() -> None:
    assert is_weather_market("Will the highest temperature in Miami be 84°F?", "", "")
    assert is_weather_market("", "highest-temperature-in-london-on-may-29-2026", "")
    assert is_weather_market("", "", "highest-temperature-in-tokyo-on-may-29-2026")
    assert not is_weather_market("Bitcoin Up or Down", "btc-updown-5m", "btc-updown-5m")
    assert not is_weather_market("Will it rain in London?", "rain-london", "rain-london")


@pytest.mark.asyncio
async def test_mock_client_weather_activity_filters() -> None:
    from polybot.polyweather.exchanges.data_api_client import MockDataApiClient

    activity = [a for a in (_parse_activity(r) for r in FIX["activity"]) if a is not None]
    client = MockDataApiClient(activity=activity)
    all_act = await client.activity()
    weather = await client.weather_activity()
    # 3 records seeded (2 weather temperature, 1 BTC) → 2 weather.
    assert len(all_act) == 3
    assert len(weather) == 2
    assert all(a.is_weather for a in weather)
