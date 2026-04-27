"""Tests for the risk manager (new API after Quarter-Kelly rewrite)."""

from __future__ import annotations

from datetime import datetime, timedelta

from polybot.config import RiskConfig
from polybot.data.models import (
    Direction,
    Order,
    OrderType,
    Portfolio,
    Position,
    PositionStatus,
    Side,
    Signal,
)
from polybot.risk.manager import RiskManager
from polybot.risk.sizing import (
    SizingPolicy,
    derive_p_win_from_signal,
    expected_value_per_dollar,
    position_size,
)


def _signal(
    *,
    target_price: float = 0.50,
    fair_value: float = 0.55,
    confidence: float = 0.7,
    edge_bps: float = 50.0,
    direction: Direction = Direction.BUY,
    size_pct: float = 0.05,
) -> Signal:
    return Signal(
        market_id="m1",
        strategy="test",
        direction=direction,
        outcome="Yes",
        target_price=target_price,
        confidence=confidence,
        size_pct=size_pct,
        reason="test",
        metadata={"fair_value": fair_value, "edge_bps": edge_bps},
    )


def _order(*, size: float = 10.0, price: float = 0.50) -> Order:
    return Order(
        market_id="m1",
        token_id="t1",
        side=Side.BUY,
        price=price,
        size=size,
        order_type=OrderType.LIMIT,
        strategy="test",
    )


# ─── Pre-trade gates ─────────────────────────────────────────────────────────


def test_can_open_position_passes_when_clean(risk_config):
    rm = RiskManager(risk_config)
    ok, reason = rm.can_open_position(Portfolio(), _signal())
    assert ok is True
    assert reason == "ok"


def test_daily_loss_halt(risk_config):
    rm = RiskManager(risk_config)
    rm.record_pnl("test", -abs(risk_config.max_daily_loss) - 1)
    ok, reason = rm.can_open_position(Portfolio(), _signal())
    assert ok is False
    assert reason == "daily_loss_halt"


def test_position_cap(risk_config):
    risk_config.max_positions = 2
    rm = RiskManager(risk_config)
    portfolio = Portfolio(
        positions=[
            Position(
                market_id=f"m{i}",
                token_id=f"t{i}",
                side=Side.BUY,
                size=10,
                avg_entry_price=0.50,
            )
            for i in range(2)
        ]
    )
    ok, reason = rm.can_open_position(portfolio, _signal())
    assert ok is False
    assert reason == "position_cap"


def test_can_place_order_rejects_oversize(risk_config):
    rm = RiskManager(risk_config)
    # max_order_size=20 default; 100 * 0.50 = $50 exceeds
    ok, reason = rm.can_place_order(_order(size=200, price=0.50), Portfolio())
    assert ok is False


# ─── Sizing ──────────────────────────────────────────────────────────────────


def test_kelly_size_for_signal_positive_edge(risk_config):
    rm = RiskManager(risk_config)
    sig = _signal(target_price=0.50, fair_value=0.60, edge_bps=200, confidence=0.8)
    result = rm.kelly_size_for_signal(sig, bankroll=500.0, timeframe="5m")
    assert result.size_usd > 0
    # 5m timeframe cap = 2% × $500 = $10; should land at or below cap
    assert result.size_usd <= 10.0 + 1e-6


def test_kelly_size_for_signal_below_edge_floor(risk_config):
    rm = RiskManager(risk_config)
    sig = _signal(edge_bps=5.0)  # below default floor of 10
    result = rm.kelly_size_for_signal(sig, bankroll=500.0, timeframe="5m")
    assert result.size_usd == 0.0
    assert result.capped_by == "edge_floor"


def test_position_size_respects_hard_cap():
    pol = SizingPolicy(bankroll_usd=500, kelly_fraction=1.0, hard_cap_pct=0.05)
    # Very high p_win to force big Kelly fraction
    r = position_size(
        bankroll=500, p_win=0.95, avg_win=0.5, avg_loss=0.5,
        edge_bps=500, confidence=1.0, timeframe="1h", policy=pol,
    )
    # With 1h cap=0.05 = $25 hits hard_cap as well
    assert r.size_usd <= 500 * 0.05 + 1e-6


def test_derive_p_win_from_signal_buy():
    p_win, avg_win, avg_loss = derive_p_win_from_signal(
        target_price=0.40, fair_value=0.60, direction_buy=True,
    )
    assert p_win == 0.60
    assert abs(avg_win - 0.60) < 1e-9   # 1 - 0.40
    assert abs(avg_loss - 0.40) < 1e-9


def test_expected_value_positive_when_kelly_positive():
    ev = expected_value_per_dollar(p_win=0.60, avg_win=0.60, avg_loss=0.40)
    assert ev > 0
