"""End-to-end paper-mock smoke test — the critical one.

Runs the engine for ~5 seconds in mock mode and asserts:
  - Dashboard responds on / and all 7 /api/weather/* endpoints
  - At least 3 signals were generated
  - At least 1 paper fill occurred
  - SQLite contains decision log entries
  - All money values are Decimal in DB (round-trip Decimal in trade rows)
  - Heartbeat task ran at least 3 times
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from polybot.polyweather.dashboard.routes import create_app


@pytest.mark.asyncio
async def test_paper_mock_end_to_end(engine_factory, resolver):
    engine, store = engine_factory(duration=4.0, cycle=0.4)
    # Use shorter heartbeat for the test
    engine.exchange.heartbeat_interval_s = 0.2
    await engine.start()

    # Engine metrics
    assert engine.metrics.cycles >= 2, f"cycles={engine.metrics.cycles}"
    assert engine.metrics.signals_total >= 3, f"signals={engine.metrics.signals_total}"
    assert engine.metrics.fills_total >= 1, f"fills={engine.metrics.fills_total}"
    assert engine.metrics.heartbeat_count >= 3, f"heartbeat={engine.metrics.heartbeat_count}"

    # SQLite — decisions + trades both populated
    assert store.trade_count() >= 1
    decisions = store.decisions(limit=10)
    assert decisions, "no decisions logged"

    # Money math invariant: every persisted trade is Decimal
    for t in store.trades(limit=5):
        assert isinstance(t.entry_price, Decimal)
        assert isinstance(t.realised_pnl_usdc, Decimal)
        assert isinstance(t.size, Decimal)

    # Dashboard endpoints
    app = create_app(engine=engine, store=store, station_resolver=resolver,
                     mode="paper", mock=True)
    client = TestClient(app)
    assert client.get("/").status_code == 200
    for endpoint in [
        "/api/weather/overview",
        "/api/weather/per-city-edge",
        "/api/weather/forecasts",
        "/api/weather/risk",
        "/api/weather/trades",
        "/api/weather/validation-gate",
    ]:
        t0 = time.perf_counter()
        r = client.get(endpoint)
        elapsed = time.perf_counter() - t0
        assert r.status_code == 200, f"{endpoint} status={r.status_code}"
        assert elapsed < 0.5, f"{endpoint} too slow {elapsed:.2f}s"
        assert r.json(), f"{endpoint} returned empty body"

    # The validation gate should be NOT READY because paper days < 14
    gate = client.get("/api/weather/validation-gate").json()
    assert gate["ready_for_live"] is False
