"""Pricing-safety regression tests.

Covers three layers of defence against the maker_edge → 1.29 blow-up:

1. maker_edge.evaluate() never emits a target_price outside [0.01, 0.99],
   even when inventory is far past max_inventory (skew normalisation).
2. RiskManager.can_place_order rejects orders with price outside (0, 1),
   for both entry and exit.
3. Storage.clear_session_data wipes orders / positions / pnl_history so
   paper mode starts at $0 P&L.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from polybot.data.models import (
    Market,
    MarketSnapshot,
    Order,
    OrderBook,
    OrderType,
    Portfolio,
    PriceLevel,
    Side,
)
from polybot.data.storage import Storage
from polybot.risk.manager import RiskManager
from polybot.strategies.maker_edge import MakerEdgeStrategy


def _snapshot(mid_yes: float = 0.075, spread: float = 0.01) -> MarketSnapshot:
    half = spread / 2
    market = Market(
        id="m-test",
        question="BTC up?",
        slug="x",
        outcomes=["Yes", "No"],
        token_ids=["tok-yes", "tok-no"],
        end_date=datetime.utcnow() + timedelta(minutes=5),
        category="crypto",
        active=True,
        volume_24h=1000.0,
        liquidity=1000.0,
    )
    ob = OrderBook(
        market_id="tok-yes",
        timestamp=datetime.utcnow(),
        bids=[PriceLevel(price=mid_yes - half, size=100.0)],
        asks=[PriceLevel(price=mid_yes + half, size=100.0)],
    )
    return MarketSnapshot(market=market, orderbook=ob, recent_trades=[])


@pytest.mark.asyncio
async def test_maker_edge_target_price_clamped_when_inventory_far_past_cap():
    """The exact regression: short ~100 shares with max_inventory=0.20
    used to produce target_price ~1.29. Must now stay in [0.01, 0.99]."""
    s = MakerEdgeStrategy(
        min_spread=0.008,
        quote_offset=0.003,
        max_inventory=0.20,
        inventory_skew=0.5,
        size_pct=0.04,
        confidence_floor=0.40,
        max_position_notional_usd=30.0,
        min_quote_interval_s=0.0,
        min_mid_change_to_requote=0.0,
        volume_boost_threshold=0.0,
    )
    s.update_inventory(market_id="m-test", delta_shares=-97.66, fill_price=0.08)

    snap = _snapshot(mid_yes=0.075, spread=0.01)
    sig = await s.evaluate(snap)

    assert sig is not None, "expected a flatten signal"
    assert 0.01 <= sig.target_price <= 0.99, (
        f"target_price {sig.target_price} outside Polymarket range — "
        "the catastrophic 1.29 regression has reappeared"
    )


@pytest.mark.asyncio
async def test_maker_edge_target_price_clamped_when_long_far_past_cap():
    s = MakerEdgeStrategy(
        max_inventory=0.20,
        inventory_skew=0.5,
        confidence_floor=0.40,
        min_quote_interval_s=0.0,
        min_mid_change_to_requote=0.0,
        volume_boost_threshold=0.0,
    )
    # 50 shares × $0.5 = $25 notional (below max_position_notional=$30 cap),
    # but still 250× max_inventory so the unclamped skew would push target
    # past 1.0 in the SELL branch.
    s.update_inventory(market_id="m-test", delta_shares=+50.0, fill_price=0.5)
    snap = _snapshot(mid_yes=0.5, spread=0.02)
    sig = await s.evaluate(snap)
    assert sig is not None
    assert 0.01 <= sig.target_price <= 0.99


def _entry_order(price: float, size: float = 10.0) -> Order:
    return Order(
        market_id="m-test",
        token_id="tok-yes",
        side=Side.BUY,
        price=price,
        size=size,
        order_type=OrderType.GTC,
        strategy="maker_edge",
    )


def test_risk_gate_rejects_price_above_one():
    rm = RiskManager.__new__(RiskManager)
    # Build a minimal RiskManager via __init__ with a default config
    from polybot.config import RiskConfig
    rm.__init__(RiskConfig())
    ok, reason = rm.can_place_order(_entry_order(price=1.29), Portfolio())
    assert not ok
    assert "price_out_of_range" in reason


def test_risk_gate_rejects_price_at_or_below_zero():
    from polybot.config import RiskConfig
    rm = RiskManager(RiskConfig())
    for bad in (0.0, -0.05, 1.0, 1.5):
        ok, reason = rm.can_place_order(_entry_order(price=bad), Portfolio())
        assert not ok, f"price {bad} should be rejected"
        assert "price_out_of_range" in reason


def test_risk_gate_rejects_out_of_range_for_exit_too():
    """Exit orders are normally bypassed, but an impossible price still
    has to be rejected — paper would simulate a bogus fill otherwise."""
    from polybot.config import RiskConfig
    rm = RiskManager(RiskConfig())
    bad_exit = Order(
        market_id="m-test",
        token_id="tok-yes",
        side=Side.SELL,
        price=1.29,
        size=10.0,
        order_type=OrderType.GTC,
        strategy="exit_maker_edge",
    )
    ok, reason = rm.can_place_order(bad_exit, Portfolio())
    assert not ok
    assert "price_out_of_range" in reason


def test_risk_gate_accepts_normal_price():
    from polybot.config import RiskConfig
    rm = RiskManager(RiskConfig())
    ok, _reason = rm.can_place_order(_entry_order(price=0.5, size=2.0), Portfolio())
    assert ok


@pytest.mark.asyncio
async def test_storage_clear_session_data_wipes_orders(tmp_path):
    db_path = str(tmp_path / "bot.db")
    s = Storage(db_path=db_path)
    await s.initialize()
    # Insert two orders
    await s.save_order({
        "order_id": "o1", "market_id": "m1", "token_id": "t1",
        "side": "BUY", "price": 0.5, "size": 10.0,
        "order_type": "GTC", "status": "FILLED",
        "strategy": "maker_edge", "signal_id": "sig1",
        "filled_size": 10.0, "avg_fill_price": 0.5,
        "created_at": "2026-05-09T00:00:00", "updated_at": "2026-05-09T00:00:00",
    })
    await s.save_order({
        "order_id": "o2", "market_id": "m2", "token_id": "t2",
        "side": "SELL", "price": 0.6, "size": 5.0,
        "order_type": "GTC", "status": "FILLED",
        "strategy": "maker_edge", "signal_id": "sig2",
        "filled_size": 5.0, "avg_fill_price": 0.6,
        "created_at": "2026-05-09T00:00:01", "updated_at": "2026-05-09T00:00:01",
    })
    cur = await s._db.execute("SELECT COUNT(*) FROM orders")
    assert (await cur.fetchone())[0] == 2

    await s.clear_session_data()

    cur = await s._db.execute("SELECT COUNT(*) FROM orders")
    assert (await cur.fetchone())[0] == 0
    await s.close()
