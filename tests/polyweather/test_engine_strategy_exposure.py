"""Engine wiring for Bug D: per-strategy exposure tracking.

The risk manager enforces a per-strategy concentration cap, but only if the
engine actually threads ``signal.strategy`` through ``can_open`` / ``record_open``
/ ``record_close``. This drives the mock engine and asserts the per-strategy
exposure map is populated and reconciles with the global open exposure.
"""

from __future__ import annotations

from decimal import Decimal

import pytest


@pytest.mark.asyncio
async def test_engine_attributes_open_exposure_to_strategies(engine_factory):
    # Hold positions well past the run window so they're still open at shutdown.
    engine, _store = engine_factory(duration=2.0, cycle=0.3, position_horizon=30.0)
    await engine.start()

    by_strategy = engine.risk.state.open_exposure_by_strategy
    assert by_strategy, "engine recorded no per-strategy exposure"
    # Every dollar of open exposure is attributed to some strategy bucket.
    total = sum(by_strategy.values(), start=Decimal("0"))
    assert abs(total - engine.risk.state.open_exposure) <= Decimal("0.01")
    # The weights were handed to the risk manager so caps can be computed.
    assert engine.risk.strategy_weights
