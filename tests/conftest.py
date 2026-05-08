"""Shared pytest fixtures."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from polybot.config import (
    AggregationConfig,
    CircuitBreakerConfig,
    ExecutionConfig,
    RiskConfig,
)
from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import (
    Direction,
    Market,
    MarketSnapshot,
    Order,
    OrderBook,
    OrderType,
    PriceLevel,
    Side,
    Signal,
)


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig(
        max_position_size=50.0,
        max_portfolio_exposure=200.0,
        max_positions=6,
        max_daily_loss=25.0,
        min_trade_interval_seconds=1,
        max_order_size=25.0,
        max_slippage_pct=5.0,
        bankroll_usd=500.0,
        kelly_fraction=0.5,
        hard_cap_pct=0.10,
        edge_floor_bps=3.0,
        min_usd=2.0,
        circuit_breakers=CircuitBreakerConfig(),
    )


@pytest.fixture
def execution_config() -> ExecutionConfig:
    return ExecutionConfig(
        rate_limit_per_second=5,
        order_ttl_seconds=15,
        retry_attempts=2,
        retry_backoff_seconds=[0.01, 0.05],
    )


@pytest.fixture
def aggregation_config() -> AggregationConfig:
    return AggregationConfig(
        min_confidence=0.30,
        conflict_resolution="weighted_vote",
        strategy_weights={
            "overshoot_reversion": 0.30,
            "boundary_decay": 0.25,
            "dual_direction_arb": 0.30,
            "maker_edge": 0.15,
        },
        min_net_score=0.15,
    )


@pytest.fixture
def make_market():
    """Factory: returns a Market with sensible defaults."""
    def _make(
        market_id: str = "m-test-1",
        question: str = "Bitcoin Up or Down — 12:30AM-12:35AM?",
        token_ids: list[str] | None = None,
        end_offset_s: float = 60.0,
        volume_24h: float = 5000.0,
        liquidity: float = 200.0,
    ) -> Market:
        if token_ids is None:
            token_ids = ["tok-yes", "tok-no"]
        return Market(
            id=market_id,
            question=question,
            slug=f"slug-{market_id}",
            outcomes=["Up", "Down"],
            token_ids=token_ids,
            end_date=datetime.utcnow() + timedelta(seconds=end_offset_s),
            category="crypto",
            active=True,
            volume_24h=volume_24h,
            liquidity=liquidity,
        )
    return _make


@pytest.fixture
def make_orderbook():
    """Factory: returns an OrderBook around a midpoint."""
    def _make(
        market_id: str = "m-test-1",
        mid: float = 0.50,
        spread: float = 0.02,
        bid_depth: float = 100.0,
        ask_depth: float = 100.0,
    ) -> OrderBook:
        bid = mid - spread / 2
        ask = mid + spread / 2
        return OrderBook(
            market_id=market_id,
            timestamp=datetime.utcnow(),
            bids=[PriceLevel(price=bid, size=bid_depth)],
            asks=[PriceLevel(price=ask, size=ask_depth)],
        )
    return _make


@pytest.fixture
def make_snapshot(make_market, make_orderbook):
    """Factory: returns a MarketSnapshot."""
    def _make(
        market_id: str = "m-test-1",
        mid: float = 0.50,
        end_offset_s: float = 60.0,
        poly_move_5s: float = 0.0,
    ) -> MarketSnapshot:
        market = make_market(market_id=market_id, end_offset_s=end_offset_s)
        ob = make_orderbook(market_id=market_id, mid=mid)
        snap = MarketSnapshot(
            market=market,
            orderbook=ob,
            recent_trades=[],
            poly_move_5s=poly_move_5s,
        )
        return snap
    return _make


@pytest.fixture
def make_signal():
    """Factory: returns a Signal with realistic metadata."""
    def _make(
        strategy: str = "overshoot_reversion",
        market_id: str = "m-test-1",
        direction: Direction = Direction.BUY,
        outcome: str = "Yes",
        target_price: float = 0.50,
        confidence: float = 0.60,
        size_pct: float = 0.05,
        edge_bps: float = 50.0,
        fair_value: float = 0.55,
        metadata: dict | None = None,
    ) -> Signal:
        meta = {"edge_bps": edge_bps, "fair_value": fair_value}
        if metadata:
            meta.update(metadata)
        return Signal(
            market_id=market_id,
            strategy=strategy,
            direction=direction,
            outcome=outcome,
            target_price=target_price,
            confidence=confidence,
            size_pct=size_pct,
            reason="test",
            metadata=meta,
        )
    return _make


@pytest.fixture
def make_order():
    """Factory: returns an Order with sensible defaults."""
    def _make(
        market_id: str = "m-test-1",
        token_id: str = "tok-yes",
        side: Side = Side.BUY,
        price: float = 0.50,
        size: float = 20.0,
        strategy: str = "overshoot_reversion",
        metadata: dict | None = None,
    ) -> Order:
        return Order(
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            order_type=OrderType.GTC,
            strategy=strategy,
            metadata=metadata or {},
        )
    return _make


@pytest.fixture
def warm_feed() -> PriceFeedState:
    """A PriceFeedState pre-populated with 120 deterministic 1Hz ticks."""
    state = PriceFeedState()
    base = 95_000.0
    now = time.time()
    for i in range(120):
        # Mild walk: ±0.05%
        price = base * (1.0 + 0.0005 * ((i % 10) - 5))
        state.push(price, ts=now - (120 - i))
    return state
