"""WeatherRiskManager: caps + kill switches."""

from __future__ import annotations

import time
from decimal import Decimal

from polybot.polyweather.risk.weather_risk import WeatherRiskConfig, WeatherRiskManager


def _mgr(mode: str = "paper") -> WeatherRiskManager:
    return WeatherRiskManager(WeatherRiskConfig(), mode=mode)


def test_position_cap_is_min_of_1pct_and_max_position_size() -> None:
    mgr = _mgr()
    cap = mgr.weather_position_cap_usdc()
    # min(1% of 1260 = 12.60, max_position_size_usdc = 12) = 12
    assert cap == Decimal("12")


def test_first_24h_live_position_cap_is_5() -> None:
    mgr = _mgr(mode="live")
    mgr.begin_live_session()
    cap = mgr.weather_position_cap_usdc()
    assert cap == Decimal("5")


def test_first_24h_cap_falls_back_to_normal_after_24h() -> None:
    mgr = _mgr(mode="live")
    mgr.state.live_session_start_ts = time.time() - 86400 * 2
    cap = mgr.weather_position_cap_usdc()
    # After 24h the $5 hard cap is gone; min(1%=12.60, max_position_size=12) = 12
    assert cap == Decimal("12")


def test_kill_switch_trips_at_50_dollars_daily_loss() -> None:
    mgr = _mgr()
    mgr.state.daily_pnl = Decimal("-51")
    halted, reason = mgr.check_kill_switch()
    assert halted
    assert "daily_loss" in reason


def test_ath_drawdown_kill_switch() -> None:
    mgr = _mgr()
    mgr.state.ath_bankroll = Decimal("1000")
    mgr.state.current_bankroll = Decimal("795")  # ~20.5% dd
    halted, reason = mgr.check_kill_switch()
    assert halted
    assert "ath_drawdown" in reason


def test_consecutive_losses_pause() -> None:
    mgr = _mgr()
    mgr.state.consecutive_losses = 5
    halted, reason = mgr.check_kill_switch()
    assert halted
    assert "consecutive_loss_pause" in reason


def test_can_open_blocks_when_over_exposure_cap() -> None:
    mgr = _mgr()
    mgr.state.open_exposure = mgr.config.max_total_open_exposure_usdc
    ok, reason = mgr.can_open(Decimal("5"))
    assert not ok
    assert "exposure" in reason


def test_kelly_size_returns_zero_for_negative_edge() -> None:
    mgr = _mgr()
    out = mgr.quarter_kelly_size(p_win=0.10, target_price=Decimal("0.50"))
    assert out == Decimal("0")


def test_kelly_size_capped_at_1pct_bankroll() -> None:
    mgr = _mgr()
    sized = mgr.quarter_kelly_size(p_win=0.95, target_price=Decimal("0.10"))
    assert sized <= mgr.weather_position_cap_usdc()
