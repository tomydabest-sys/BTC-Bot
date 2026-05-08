"""Alert channels for notifications (Discord, Telegram)."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod

import httpx
import structlog

from polybot.data.models import AlertLevel

logger = structlog.get_logger()


class AlertChannel(ABC):
    @abstractmethod
    async def send(self, level: AlertLevel, message: str, data: dict) -> None: ...


class DiscordWebhookChannel(AlertChannel):
    """Send alerts via Discord webhook."""

    def __init__(self, webhook_url_env: str = "DISCORD_WEBHOOK_URL") -> None:
        self._webhook_url = os.environ.get(webhook_url_env, "")

    async def send(self, level: AlertLevel, message: str, data: dict) -> None:
        if not self._webhook_url:
            return
        color = {"INFO": 0x00FF00, "WARNING": 0xFFFF00, "CRITICAL": 0xFF0000}.get(
            level.value, 0x808080
        )
        payload = {
            "embeds": [
                {
                    "title": f"[{level.value}] Polymarket Bot",
                    "description": message,
                    "color": color,
                    "fields": [{"name": k, "value": str(v), "inline": True} for k, v in data.items()],
                }
            ]
        }
        try:
            async with httpx.AsyncClient() as client:
                await client.post(self._webhook_url, json=payload)
        except Exception as e:
            logger.error("discord_alert_failed", error=str(e))


class TelegramChannel(AlertChannel):
    """Send alerts via Telegram bot."""

    def __init__(
        self,
        bot_token_env: str = "TELEGRAM_BOT_TOKEN",
        chat_id_env: str = "TELEGRAM_CHAT_ID",
    ) -> None:
        self._bot_token = os.environ.get(bot_token_env, "")
        self._chat_id = os.environ.get(chat_id_env, "")

    async def send(self, level: AlertLevel, message: str, data: dict) -> None:
        if not self._bot_token or not self._chat_id:
            return
        icon = {"INFO": "ℹ️", "WARNING": "⚠️", "CRITICAL": "🚨"}.get(level.value, "📢")
        text = f"{icon} *{level.value}*\n{message}"
        if data:
            text += "\n" + "\n".join(f"• {k}: `{v}`" for k, v in data.items())
        try:
            url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
            async with httpx.AsyncClient() as client:
                await client.post(url, json={"chat_id": self._chat_id, "text": text, "parse_mode": "Markdown"})
        except Exception as e:
            logger.error("telegram_alert_failed", error=str(e))


class LogChannel(AlertChannel):
    """Fallback: log alerts to structured logger."""

    async def send(self, level: AlertLevel, message: str, data: dict) -> None:
        logger.info("alert", level=level.value, message=message, **data)


class AlertManager:
    """Routes alerts to configured channels.

    Accepts either a list of AlertChannel instances or an AlertsConfig
    pydantic model (the orchestrator passes the latter). When an AlertsConfig
    is supplied, channels are constructed lazily based on which env vars are
    populated; LogChannel is always included as a fallback.
    """

    def __init__(self, channels_or_config=None) -> None:
        if channels_or_config is None:
            self._channels: list[AlertChannel] = [LogChannel()]
        elif isinstance(channels_or_config, list):
            self._channels = channels_or_config or [LogChannel()]
        else:
            cfg = channels_or_config
            built: list[AlertChannel] = [LogChannel()]
            try:
                if os.environ.get(getattr(cfg, "discord_webhook_env", "DISCORD_WEBHOOK_URL"), ""):
                    built.append(DiscordWebhookChannel(cfg.discord_webhook_env))
                if (
                    os.environ.get(getattr(cfg, "telegram_bot_token_env", "TELEGRAM_BOT_TOKEN"), "")
                    and os.environ.get(getattr(cfg, "telegram_chat_id_env", "TELEGRAM_CHAT_ID"), "")
                ):
                    built.append(
                        TelegramChannel(cfg.telegram_bot_token_env, cfg.telegram_chat_id_env)
                    )
            except Exception as e:
                logger.warning("alerts_config_parse_err", error=str(e))
            self._channels = built
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def send_alert(self, level: AlertLevel, message: str, data: dict | None = None) -> None:
        data = data or {}
        for channel in self._channels:
            try:
                await channel.send(level, message, data)
            except Exception as e:
                logger.error("alert_channel_error", channel=type(channel).__name__, error=str(e))
