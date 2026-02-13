"""Shared test fixtures."""

from datetime import datetime, timedelta

import pytest

from polybot.config import Config, RiskConfig
from polybot.data.models import (
    Market,
    OrderBook,
    PriceLevel,
    Trade,
    MarketSnapshot,
    Side,
)


@pytest.fixture
def sample_market() -> Market:
    return Market(
        id="test_market_1",
        question="Will BTC reach $100k by end of 2026?",
        slug="btc-100k-2026",
        outcomes=["Yes", "No"],
        token_ids=["token_yes_1", "token_no_1"],
        end_date=datetime.utcnow() + timedelta(days=15),
        category="crypto",
        active=True,
        volume_24h=50000,
        liquidity=20000,
    )


@pytest.fixture
def sample_orderbook() -> OrderBook:
    return OrderBook(
        market_id="test_market_1",
        timestamp=datetime.utcnow(),
        bids=[
            PriceLevel(0.55, 1000),
            PriceLevel(0.54, 2000),
            PriceLevel(0.53, 3000),
        ],
        asks=[
            PriceLevel(0.57, 1000),
            PriceLevel(0.58, 2000),
            PriceLevel(0.59, 3000),
        ],
    )


@pytest.fixture
def sample_snapshot(sample_market, sample_orderbook) -> MarketSnapshot:
    trades = [
        Trade(
            market_id="test_market_1",
            timestamp=datetime.utcnow() - timedelta(minutes=i),
            side=Side.BUY,
            price=0.55 + (i % 5) * 0.01,
            size=100,
            outcome="Yes",
        )
        for i in range(20)
    ]
    return MarketSnapshot(
        market=sample_market,
        orderbook=sample_orderbook,
        recent_trades=trades,
        vwap_1h=0.56,
        vwap_24h=0.55,
        volatility_1h=0.02,
        price_history=[0.50 + i * 0.005 for i in range(20)],
    )


@pytest.fixture
def default_config() -> Config:
    return Config()


@pytest.fixture
def risk_config() -> RiskConfig:
    return RiskConfig()
