"""All 7 /api/weather/* endpoints + frontend root."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from polybot.polyweather.dashboard.routes import create_app


@pytest.fixture
def engine_with_data(engine_factory):
    engine, store = engine_factory(duration=1.5, cycle=0.3)
    asyncio.run(engine.start())
    return engine, store


def test_root_serves_html(engine_with_data, resolver):
    engine, store = engine_with_data
    app = create_app(engine=engine, store=store, station_resolver=resolver, mode="paper", mock=True)
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "PolyWeather-Bot" in r.text


@pytest.mark.parametrize("endpoint", [
    "/api/weather/overview",
    "/api/weather/per-city-edge",
    "/api/weather/forecasts",
    "/api/weather/risk",
    "/api/weather/trades",
    "/api/weather/validation-gate",
])
def test_endpoints_200_under_500ms(endpoint, engine_with_data, resolver):
    engine, store = engine_with_data
    app = create_app(engine=engine, store=store, station_resolver=resolver, mode="paper", mock=True)
    client = TestClient(app)
    t0 = time.perf_counter()
    r = client.get(endpoint)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, f"{endpoint} status={r.status_code}"
    assert elapsed < 0.5, f"{endpoint} took {elapsed:.3f}s"
    assert r.json()  # not empty


def test_overview_has_required_keys(engine_with_data, resolver):
    engine, store = engine_with_data
    app = create_app(engine=engine, store=store, station_resolver=resolver, mode="paper", mock=True)
    client = TestClient(app)
    payload = client.get("/api/weather/overview").json()
    for key in ("bankroll_usdc", "pnl_24h_usdc", "equity_curve", "heartbeat", "mode"):
        assert key in payload, f"missing key {key}"


def test_validation_gate_blocks_when_low_data(engine_with_data, resolver):
    engine, store = engine_with_data
    app = create_app(engine=engine, store=store, station_resolver=resolver, mode="paper", mock=True)
    client = TestClient(app)
    payload = client.get("/api/weather/validation-gate").json()
    assert payload["ready_for_live"] is False
    assert "trade_count" in payload["criteria"]


def test_market_detail_endpoint(engine_with_data, resolver):
    engine, store = engine_with_data
    app = create_app(engine=engine, store=store, station_resolver=resolver, mode="paper", mock=True)
    client = TestClient(app)
    # Pick a real market id from decisions
    decisions = store.decisions(limit=5)
    if not decisions:
        pytest.skip("no decisions written yet")
    mid = decisions[0]["market_id"]
    r = client.get(f"/api/weather/market/{mid}")
    assert r.status_code == 200
    assert r.json()["market_id"] == mid


def test_decimal_serialised_to_string_not_typeerror(engine_with_data, resolver):
    engine, store = engine_with_data
    app = create_app(engine=engine, store=store, station_resolver=resolver, mode="paper", mock=True)
    client = TestClient(app)
    payload = client.get("/api/weather/overview").json()
    # bankroll comes back as a string-encoded Decimal
    assert isinstance(payload["bankroll_usdc"], str)
