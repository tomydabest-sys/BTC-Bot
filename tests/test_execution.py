"""Tests for ExecutionEngine — dual-leg paper, retry, live-mode hard-guard."""

from __future__ import annotations

import asyncio

import pytest

from polybot.data.client import (
    LIVE_TRADING_ENABLED,
    LiveTradingNotImplementedError,
    PolymarketClient,
)
from polybot.data.models import (
    Order,
    OrderStatus,
    OrderType,
    Portfolio,
    Side,
)
from polybot.events import EventBus
from polybot.execution.engine import ExecutionEngine
from polybot.risk.manager import RiskManager


def _make_engine(risk_config, execution_config, is_paper: bool = True):
    bus = EventBus()
    client = PolymarketClient(api_key="", private_key="")
    rm = RiskManager(risk_config)
    eng = ExecutionEngine(
        client=client,
        risk_manager=rm,
        config=execution_config,
        event_bus=bus,
        is_paper=is_paper,
    )
    return eng, bus


@pytest.mark.asyncio
class TestPaperExecution:
    async def test_simple_paper_fill(self, risk_config, execution_config, make_order):
        engine, bus = _make_engine(risk_config, execution_config)
        events: list = []
        bus.subscribe("order_filled", lambda **kw: events.append(kw["order"]))

        order = make_order(price=0.5, size=20)
        out = await engine.execute_order(order, Portfolio())
        await asyncio.sleep(0.01)

        assert out.status == OrderStatus.FILLED
        assert out.filled_size == 20
        assert out.avg_fill_price == 0.5
        assert any(e.market_id == order.market_id for e in events)

    async def test_dual_leg_paper_emits_both_fills(
        self, risk_config, execution_config, make_order,
    ):
        engine, bus = _make_engine(risk_config, execution_config)
        fills: list = []
        bus.subscribe("order_filled", lambda **kw: fills.append(kw["order"]))

        yes_order = make_order(
            price=0.55, size=20, strategy="dual_direction_arb",
            metadata={
                "is_dual_direction": True,
                "no_token_id": "tok-no",
                "no_implied_ask": 0.45,
                "legs_max_age_ms": 500,
            },
        )
        out = await engine.execute_order(yes_order, Portfolio())
        await asyncio.sleep(0.01)

        assert out.status == OrderStatus.FILLED
        # Two fills should have been emitted: yes leg + no leg
        assert len(fills) == 2

    async def test_dual_leg_missing_metadata_rejected(
        self, risk_config, execution_config, make_order,
    ):
        engine, _bus = _make_engine(risk_config, execution_config)
        # Missing no_token_id in metadata
        yes_order = make_order(
            price=0.55, size=20, strategy="dual_direction_arb",
            metadata={"is_dual_direction": True},
        )
        out = await engine.execute_order(yes_order, Portfolio())
        assert out.status == OrderStatus.REJECTED


@pytest.mark.asyncio
class TestLiveModeHardGuard:
    async def test_live_place_raises_not_implemented(
        self, risk_config, execution_config, make_order,
    ):
        """In paper mode this is fine; live mode should hit the guard."""
        # Construct a *live* engine so place_order is actually called
        engine, _bus = _make_engine(risk_config, execution_config, is_paper=False)
        order = make_order(price=0.5, size=20)
        out = await engine.execute_order(order, Portfolio())
        # Should be rejected (live not implemented yet)
        assert out.status == OrderStatus.REJECTED

    async def test_client_place_order_raises_clearly(self):
        client = PolymarketClient(api_key="", private_key="")
        await client.start()
        try:
            order = Order(
                market_id="m-test",
                token_id="t-yes",
                side=Side.BUY,
                price=0.50,
                size=10,
                order_type=OrderType.GTC,
                strategy="test",
            )
            with pytest.raises(LiveTradingNotImplementedError):
                await client.place_order(order)
        finally:
            await client.close()


@pytest.mark.asyncio
class TestExitOrderBypass:
    async def test_oversized_exit_passes(
        self, risk_config, execution_config, make_order,
    ):
        """Risk gate should let exit orders through even when oversized."""
        engine, _bus = _make_engine(risk_config, execution_config)
        order = make_order(
            price=0.5, size=200,  # $100 notional > max_order_size=$25
            strategy="exit_overshoot_reversion",
        )
        out = await engine.execute_order(order, Portfolio())
        assert out.status == OrderStatus.FILLED


@pytest.mark.asyncio
class TestRiskBlockedRejection:
    async def test_oversized_entry_rejected(
        self, risk_config, execution_config, make_order,
    ):
        engine, _bus = _make_engine(risk_config, execution_config)
        order = make_order(price=0.5, size=200)  # $100 > $25 max_order_size
        out = await engine.execute_order(order, Portfolio())
        assert out.status == OrderStatus.CANCELLED
