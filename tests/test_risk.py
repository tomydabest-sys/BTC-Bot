"""Tests for the risk manager."""

from datetime import datetime

from polybot.config import RiskConfig
from polybot.data.models import (
    Order,
    OrderType,
    Portfolio,
    Position,
    RiskDecision,
    Side,
)
from polybot.risk.manager import RiskManager


def test_approve_valid_order(risk_config):
    rm = RiskManager(risk_config)
    order = Order(
        market_id="m1",
        token_id="t1",
        side=Side.BUY,
        price=0.50,
        size=100,
        order_type=OrderType.LIMIT,
        strategy="test",
    )
    portfolio = Portfolio()
    result = rm.check_order(order, portfolio)
    assert result.decision == RiskDecision.APPROVE


def test_reject_exceeds_daily_loss(risk_config):
    rm = RiskManager(risk_config)
    order = Order(
        market_id="m1",
        token_id="t1",
        side=Side.BUY,
        price=0.50,
        size=100,
        order_type=OrderType.LIMIT,
        strategy="test",
    )
    portfolio = Portfolio(daily_pnl=-300)  # Exceeds default $250 limit
    result = rm.check_order(order, portfolio)
    assert result.decision == RiskDecision.REJECT


def test_reduce_exceeds_order_size(risk_config):
    rm = RiskManager(risk_config)
    order = Order(
        market_id="m1",
        token_id="t1",
        side=Side.BUY,
        price=0.50,
        size=500,  # 500 * 0.50 = $250, exceeds $200 max
        order_type=OrderType.LIMIT,
        strategy="test",
    )
    portfolio = Portfolio()
    result = rm.check_order(order, portfolio)
    assert result.decision == RiskDecision.REDUCE
    assert result.modified_size is not None
    assert result.modified_size * order.price <= risk_config.max_order_size


def test_reject_max_positions(risk_config):
    risk_config.max_positions = 2
    rm = RiskManager(risk_config)
    order = Order(
        market_id="m3",
        token_id="t3",
        side=Side.BUY,
        price=0.50,
        size=10,
        order_type=OrderType.LIMIT,
        strategy="test",
    )
    portfolio = Portfolio(
        positions=[
            Position(market_id="m1", token_id="t1", outcome="Yes", side=Side.BUY, size=10, avg_entry_price=0.50),
            Position(market_id="m2", token_id="t2", outcome="Yes", side=Side.BUY, size=10, avg_entry_price=0.50),
        ]
    )
    result = rm.check_order(order, portfolio)
    assert result.decision == RiskDecision.REJECT
