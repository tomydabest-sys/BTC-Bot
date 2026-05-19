"""Tests for the ATH drawdown kill switch in RiskManager."""

from __future__ import annotations

import pytest

from polybot.config import CircuitBreakerConfig, RiskConfig
from polybot.data.models import Direction, Portfolio, Signal
from polybot.risk.manager import RiskManager


@pytest.fixture
def signal() -> Signal:
    return Signal(
        market_id="m-1",
        strategy="overshoot_reversion",
        direction=Direction.BUY,
        outcome="Yes",
        target_price=0.55,
        confidence=0.7,
        size_pct=0.05,
        reason="test",
        metadata={"edge_bps": 50, "fair_value": 0.60},
    )


def _make_rm(ath_pct: float) -> RiskManager:
    cfg = RiskConfig(
        bankroll_usd=500.0,
        ath_drawdown_kill_pct=ath_pct,
        max_position_size=50,
        max_portfolio_exposure=200,
        max_positions=6,
        max_daily_loss=25,
        circuit_breakers=CircuitBreakerConfig(),
    )
    return RiskManager(cfg)


def test_disabled_when_threshold_zero(signal):
    rm = _make_rm(0.0)
    rm.record_equity(500)
    rm.record_equity(100)  # massive drawdown
    assert rm.ath_killed is False
    ok, reason = rm.can_open_position(Portfolio(), signal)
    assert ok is True


def test_kill_triggers_at_threshold(signal):
    rm = _make_rm(0.40)
    # New peak
    rm.record_equity(600)
    assert rm.peak_equity == 600
    # 39% drawdown — not yet
    rm.record_equity(366)
    assert rm.ath_killed is False
    # 40% drawdown — trips
    rm.record_equity(360)
    assert rm.ath_killed is True
    ok, reason = rm.can_open_position(Portfolio(), signal)
    assert ok is False
    assert reason == "ath_drawdown_kill"


def test_kill_latches_even_when_equity_recovers(signal):
    rm = _make_rm(0.40)
    rm.record_equity(600)
    rm.record_equity(300)  # tripped
    assert rm.ath_killed is True
    rm.record_equity(550)  # recovery
    assert rm.ath_killed is True  # still locked
    ok, _ = rm.can_open_position(Portfolio(), signal)
    assert ok is False


def test_peak_equity_only_increases(signal):
    rm = _make_rm(0.40)
    rm.record_equity(500)
    assert rm.peak_equity == 500
    rm.record_equity(400)  # no peak update
    assert rm.peak_equity == 500
    rm.record_equity(700)  # new peak
    assert rm.peak_equity == 700


def test_manual_reset_clears_kill(signal):
    rm = _make_rm(0.40)
    rm.record_equity(600)
    rm.record_equity(300)
    assert rm.ath_killed is True
    rm.reset_ath_kill()
    assert rm.ath_killed is False
    ok, _ = rm.can_open_position(Portfolio(), signal)
    assert ok is True


def test_bot_status_loop_ticks_record_equity(tmp_path):
    """Regression: prior to Phase 4 the ATH kill switch was defined but
    never called from the running bot. Confirm `_emit_status` now feeds
    equity into RiskManager.record_equity on every tick."""
    from polybot.config import (
        BotConfig,
        Config,
        MakerConfig,
    )
    from polybot.main import Bot

    cfg = Config(
        bot=BotConfig(mode="paper", data_dir=str(tmp_path / "data")),
        maker=MakerConfig(enabled=False),
    )
    # Set the threshold so we can verify the wiring trips at the right point.
    cfg.risk.ath_drawdown_kill_pct = 0.40
    cfg.risk.bankroll_usd = 500.0
    bot = Bot(cfg)
    assert bot._risk.peak_equity == 500.0
    assert bot._risk.ath_killed is False

    # First tick at par — should set peak.
    bot._emit_status()
    assert bot._risk.peak_equity >= 500.0

    # Engineer a 40% drawdown via realized P&L. _effective_bankroll returns
    # config.risk.bankroll_usd; the equity sample = bankroll + realized_pnl.
    bot._positions.portfolio.realized_pnl = -200.0  # equity = 300
    bot._emit_status()
    assert bot._risk.ath_killed is True, (
        "ATH kill switch must trip when _emit_status ticks record_equity"
    )
