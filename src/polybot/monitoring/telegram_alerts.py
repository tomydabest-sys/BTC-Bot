"""Telegram heartbeat + critical alerts.

Builds on the existing `alerts.TelegramChannel` (which sends a single
message) and adds:

  * a periodic hourly heartbeat with bot vitals, and
  * convenience helpers for the critical alert triggers the brief calls
    out (drawdown stop, ATH kill, feed down, fee-rate change, latency
    breach, unhandled exception).

If the Telegram bot token / chat id env vars are missing, every method
short-circuits to a no-op so importing this module is always safe.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

import structlog

from polybot.data.models import AlertLevel
from polybot.monitoring.alerts import AlertManager, TelegramChannel

logger = structlog.get_logger()


@dataclass
class HeartbeatVitals:
    bankroll_usd: float = 0.0
    quote_uptime_pct: float = 0.0
    fills_today: int = 0
    pnl_today_usd: float = 0.0
    net_inventory_shares: float = 0.0
    p95_latency_ms: float = 0.0


class TelegramAlerter:
    """Wraps AlertManager for maker-mode operational alerts."""

    def __init__(
        self,
        manager: AlertManager | None = None,
        *,
        bot_token_env: str = "TELEGRAM_BOT_TOKEN",
        chat_id_env: str = "TELEGRAM_CHAT_ID",
        heartbeat_interval_s: float = 3600.0,
    ) -> None:
        self._heartbeat_interval_s = heartbeat_interval_s
        if manager is None:
            manager = AlertManager()
            if os.environ.get(bot_token_env) and os.environ.get(chat_id_env):
                manager._channels.append(
                    TelegramChannel(bot_token_env, chat_id_env)
                )
        self._manager = manager
        self._heartbeat_task: asyncio.Task | None = None
        self._vitals_provider = None  # type: ignore[assignment]
        self._last_heartbeat = 0.0
        self._sent_alerts: dict[str, float] = {}

    # ─────────────────────────────────────────────────────────────────
    #  Heartbeat
    # ─────────────────────────────────────────────────────────────────

    def set_vitals_provider(self, fn) -> None:  # type: ignore[no-untyped-def]
        """Inject a callable that returns a fresh HeartbeatVitals."""
        self._vitals_provider = fn

    async def start(self) -> None:
        if self._heartbeat_task is not None:
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._heartbeat_interval_s)
                await self.send_heartbeat()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("telegram_heartbeat_err", err=str(e)[:80])

    async def send_heartbeat(self) -> None:
        vitals = None
        if self._vitals_provider is not None:
            try:
                vitals = self._vitals_provider()
            except Exception as e:
                logger.warning("vitals_provider_failed", err=str(e)[:80])
        if vitals is None:
            vitals = HeartbeatVitals()
        data = {
            "bankroll": f"${vitals.bankroll_usd:.2f}",
            "uptime": f"{vitals.quote_uptime_pct:.0f}%",
            "fills": vitals.fills_today,
            "pnl": f"${vitals.pnl_today_usd:+.2f}",
            "inv": f"{vitals.net_inventory_shares:+.1f}",
            "p95_ms": f"{vitals.p95_latency_ms:.0f}",
        }
        await self._manager.send_alert(AlertLevel.INFO, "Heartbeat", data)
        self._last_heartbeat = time.monotonic()

    # ─────────────────────────────────────────────────────────────────
    #  Critical alerts (dedup'd by key, 5-min repeat throttle)
    # ─────────────────────────────────────────────────────────────────

    async def _send_once(
        self,
        key: str,
        level: AlertLevel,
        message: str,
        data: dict[str, object] | None = None,
        *,
        repeat_after_s: float = 300.0,
    ) -> None:
        now = time.monotonic()
        last = self._sent_alerts.get(key)
        if last is not None and (now - last) < repeat_after_s:
            return
        self._sent_alerts[key] = now
        await self._manager.send_alert(level, message, data or {})

    async def drawdown_stop(self, *, drawdown_pct: float) -> None:
        await self._send_once(
            "drawdown_stop",
            AlertLevel.CRITICAL,
            "Daily drawdown stop hit — trading halted 24h",
            {"drawdown": f"{drawdown_pct:.1%}"},
        )

    async def ath_kill(self, *, drawdown_pct: float) -> None:
        await self._send_once(
            "ath_kill",
            AlertLevel.CRITICAL,
            "ATH drawdown kill triggered — permanent halt, manual restart",
            {"drawdown": f"{drawdown_pct:.1%}"},
        )

    async def binance_feed_down(self, *, age_s: float) -> None:
        await self._send_once(
            "binance_down",
            AlertLevel.WARNING,
            "Binance feed silent — quotes cancelled",
            {"age_s": f"{age_s:.0f}"},
        )

    async def clob_ws_down(self, *, age_s: float) -> None:
        await self._send_once(
            "clob_ws_down",
            AlertLevel.WARNING,
            "Polymarket CLOB WS down — quotes cancelled",
            {"age_s": f"{age_s:.0f}"},
        )

    async def unhandled_exception(self, *, where: str, err: str) -> None:
        await self._send_once(
            f"exc:{where}",
            AlertLevel.CRITICAL,
            f"Unhandled exception in {where}",
            {"error": err[:200]},
        )

    async def fee_rate_changed(
        self, *, token_id: str, old: float, new: float
    ) -> None:
        await self._send_once(
            f"fee_change:{token_id[:8]}",
            AlertLevel.WARNING,
            "Fee rate changed mid-session",
            {"market": token_id[:12], "old": old, "new": new},
        )

    async def latency_breach(self, *, p95_ms: float) -> None:
        await self._send_once(
            "latency_breach",
            AlertLevel.CRITICAL,
            "Cancel/replace p95 latency breached kill threshold",
            {"p95_ms": f"{p95_ms:.0f}"},
        )
