"""DataPipeline tests — per-token book isolation.

Regression test for the cross-token exit-pricing bug: in a binary market
the YES and NO books are roughly mirror images (~1.0 apart). If the
pipeline only retains the most recently updated book, an exit that reads
the wrong side closes the position at ~the inverse price. Real-world
observed PnL: SHORT YES @ 0.07 closed at NO's ask 0.96, realising −$317
on a $25 position before the daily-loss circuit breaker tripped.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from polybot.data.models import Market
from polybot.data.pipeline import DataPipeline
from polybot.events import EventBus


def _market() -> Market:
    return Market(
        id="m-test",
        question="BTC up?",
        slug="x",
        outcomes=["Yes", "No"],
        token_ids=["tok-yes", "tok-no"],
        end_date=datetime.utcnow() + timedelta(minutes=10),
        category="crypto",
        active=True,
        volume_24h=100.0,
        liquidity=100.0,
    )


@pytest.mark.asyncio
async def test_per_token_orderbooks_do_not_overwrite_each_other():
    bus = EventBus()
    pipe = DataPipeline(event_bus=bus)
    await pipe.start()
    pipe.register_market(_market())

    await bus.emit("orderbook_update", data={
        "asset_id": "tok-yes",
        "bids": [{"price": "0.04", "size": "100"}],
        "asks": [{"price": "0.07", "size": "100"}],
    })
    await bus.emit("orderbook_update", data={
        "asset_id": "tok-no",
        "bids": [{"price": "0.93", "size": "100"}],
        "asks": [{"price": "0.96", "size": "100"}],
    })

    yes_ob = pipe.get_orderbook("m-test", "tok-yes")
    no_ob = pipe.get_orderbook("m-test", "tok-no")
    assert yes_ob is not None and no_ob is not None
    assert yes_ob.best_bid == 0.04
    assert yes_ob.best_ask == 0.07
    assert no_ob.best_bid == 0.93
    assert no_ob.best_ask == 0.96

    await pipe.stop()


@pytest.mark.asyncio
async def test_snapshot_orderbook_is_primary_token_regardless_of_update_order():
    """MarketSnapshot.orderbook must be the primary (first) token's book —
    strategies rely on a stable view, not whichever side last emitted."""
    bus = EventBus()
    pipe = DataPipeline(event_bus=bus)
    await pipe.start()
    pipe.register_market(_market())

    # NO book updates first
    await bus.emit("orderbook_update", data={
        "asset_id": "tok-no",
        "bids": [{"price": "0.93", "size": "100"}],
        "asks": [{"price": "0.96", "size": "100"}],
    })
    # then YES
    await bus.emit("orderbook_update", data={
        "asset_id": "tok-yes",
        "bids": [{"price": "0.04", "size": "100"}],
        "asks": [{"price": "0.07", "size": "100"}],
    })
    # NO updates again — should not pollute the snapshot
    await bus.emit("orderbook_update", data={
        "asset_id": "tok-no",
        "bids": [{"price": "0.94", "size": "100"}],
        "asks": [{"price": "0.95", "size": "100"}],
    })

    snap = pipe.get_snapshot("m-test")
    assert snap is not None
    assert snap.orderbook.best_bid == 0.04
    assert snap.orderbook.best_ask == 0.07

    await pipe.stop()


@pytest.mark.asyncio
async def test_get_orderbook_returns_none_for_unknown_token():
    bus = EventBus()
    pipe = DataPipeline(event_bus=bus)
    await pipe.start()
    pipe.register_market(_market())

    assert pipe.get_orderbook("m-test", "tok-yes") is None  # no frame yet
    assert pipe.get_orderbook("m-unknown", "tok-yes") is None

    await pipe.stop()


@pytest.mark.asyncio
async def test_unregister_clears_per_token_state():
    bus = EventBus()
    pipe = DataPipeline(event_bus=bus)
    await pipe.start()
    pipe.register_market(_market())

    await bus.emit("orderbook_update", data={
        "asset_id": "tok-yes",
        "bids": [{"price": "0.04", "size": "100"}],
        "asks": [{"price": "0.07", "size": "100"}],
    })
    assert pipe.get_orderbook("m-test", "tok-yes") is not None

    pipe.unregister_market("m-test")
    assert pipe.get_orderbook("m-test", "tok-yes") is None
    assert pipe.get_market("m-test") is None
    assert pipe.get_snapshot("m-test") is None

    await pipe.stop()
