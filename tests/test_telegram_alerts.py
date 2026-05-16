"""Tests for the Telegram alerter — runs entirely against the LogChannel
fallback so no network is required."""

from __future__ import annotations

import asyncio

import pytest

from polybot.data.models import AlertLevel
from polybot.monitoring.alerts import AlertChannel, AlertManager
from polybot.monitoring.telegram_alerts import HeartbeatVitals, TelegramAlerter


class _CaptureChannel(AlertChannel):
    def __init__(self) -> None:
        self.sent: list[tuple[AlertLevel, str, dict]] = []

    async def send(self, level: AlertLevel, message: str, data: dict) -> None:
        self.sent.append((level, message, data))


@pytest.fixture
def alerter_and_capture():
    cap = _CaptureChannel()
    manager = AlertManager([cap])
    return TelegramAlerter(manager, heartbeat_interval_s=999_999.0), cap


@pytest.mark.asyncio
async def test_heartbeat_sends_with_provider(alerter_and_capture):
    alerter, cap = alerter_and_capture

    def vitals() -> HeartbeatVitals:
        return HeartbeatVitals(
            bankroll_usd=512.0,
            quote_uptime_pct=87.5,
            fills_today=14,
            pnl_today_usd=4.21,
            net_inventory_shares=-12,
            p95_latency_ms=82,
        )

    alerter.set_vitals_provider(vitals)
    await alerter.send_heartbeat()
    assert len(cap.sent) == 1
    level, msg, data = cap.sent[0]
    assert level == AlertLevel.INFO
    assert "Heartbeat" in msg
    assert data["bankroll"] == "$512.00"
    assert data["pnl"] == "$+4.21"


@pytest.mark.asyncio
async def test_critical_alerts_dedup_within_window(alerter_and_capture):
    alerter, cap = alerter_and_capture
    await alerter.drawdown_stop(drawdown_pct=0.12)
    await alerter.drawdown_stop(drawdown_pct=0.13)
    await alerter.drawdown_stop(drawdown_pct=0.14)
    assert len(cap.sent) == 1
    assert cap.sent[0][0] == AlertLevel.CRITICAL


@pytest.mark.asyncio
async def test_distinct_alert_keys_send_separately(alerter_and_capture):
    alerter, cap = alerter_and_capture
    await alerter.drawdown_stop(drawdown_pct=0.12)
    await alerter.ath_kill(drawdown_pct=0.45)
    await alerter.binance_feed_down(age_s=12)
    assert len(cap.sent) == 3


@pytest.mark.asyncio
async def test_start_stop_runs_clean(alerter_and_capture):
    alerter, _ = alerter_and_capture
    await alerter.start()
    # Heartbeat loop is asleep on a huge interval; stop should cancel cleanly.
    await asyncio.sleep(0)
    await alerter.stop()
