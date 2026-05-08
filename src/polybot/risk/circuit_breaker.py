"""Circuit breakers for automated risk control.

PATCHED: now fires alerts on state changes (pause, daily halt, size reduction)
through an optional AlertManager hook. Previously these events only logged.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
from typing import Optional

import structlog

from polybot.config import CircuitBreakerConfig
from polybot.data.models import AlertLevel

logger = structlog.get_logger()


class CircuitBreaker:
    """Monitors adverse conditions and halts trading when triggered."""

    def __init__(
        self,
        config: CircuitBreakerConfig,
        alert_manager=None,  # Optional[AlertManager] — avoid circular import
    ) -> None:
        self._config = config
        self._consecutive_losses = 0
        self._api_errors: deque[datetime] = deque()
        self._paused_until: datetime | None = None
        self._size_multiplier = 1.0
        self._alert_manager = alert_manager

    def set_alert_manager(self, alert_manager) -> None:
        """Wire AlertManager post-construction (avoids circular import)."""
        self._alert_manager = alert_manager

    @property
    def is_trading_allowed(self) -> bool:
        if self._paused_until and datetime.utcnow() < self._paused_until:
            return False
        return True

    @property
    def size_multiplier(self) -> float:
        return self._size_multiplier

    def record_trade_result(self, pnl: float) -> None:
        if pnl < 0:
            self._consecutive_losses += 1
            if self._consecutive_losses >= self._config.consecutive_losses_pause:
                old_mult = self._size_multiplier
                self._size_multiplier *= self._config.consecutive_losses_size_reduction
                logger.warning(
                    "circuit_breaker_size_reduction",
                    consecutive_losses=self._consecutive_losses,
                    new_multiplier=self._size_multiplier,
                )
                self._fire_alert(
                    AlertLevel.WARNING,
                    "Position size reduced after consecutive losses",
                    {
                        "consecutive_losses": self._consecutive_losses,
                        "old_multiplier": round(old_mult, 3),
                        "new_multiplier": round(self._size_multiplier, 3),
                    },
                )
        else:
            self._consecutive_losses = 0
            self._size_multiplier = min(self._size_multiplier * 1.1, 1.0)

    def record_api_error(self) -> None:
        now = datetime.utcnow()
        self._api_errors.append(now)
        cutoff = now - timedelta(minutes=1)
        while self._api_errors and self._api_errors[0] < cutoff:
            self._api_errors.popleft()

        if len(self._api_errors) >= self._config.api_errors_per_minute_pause:
            self._pause_trading(timedelta(minutes=5), "Too many API errors")
            self._fire_alert(
                AlertLevel.CRITICAL,
                "Circuit breaker tripped: API error rate too high",
                {"errors_per_minute": len(self._api_errors)},
            )

    def record_daily_loss_breach(self) -> None:
        now = datetime.utcnow()
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self._paused_until = tomorrow
        logger.critical("circuit_breaker_daily_halt", resume_at=tomorrow.isoformat())
        self._fire_alert(
            AlertLevel.CRITICAL,
            "Daily loss circuit breaker activated — trading halted",
            {"resume_at": tomorrow.isoformat()},
        )

    def _pause_trading(self, duration: timedelta, reason: str) -> None:
        self._paused_until = datetime.utcnow() + duration
        logger.warning(
            "circuit_breaker_pause",
            reason=reason,
            resume_at=self._paused_until.isoformat(),
        )

    def _fire_alert(self, level: AlertLevel, message: str, data: dict) -> None:
        """Fire an alert if AlertManager wired up. Best-effort — never raises."""
        if self._alert_manager is None:
            return
        try:
            # Run as background task so we don't block the trading loop
            asyncio.create_task(
                self._alert_manager.send_alert(level, message, data)
            )
        except RuntimeError:
            # No running event loop — caller is in non-async context.
            # Safe to silently skip; the log line above still records the event.
            pass
        except Exception as e:
            logger.debug("circuit_breaker_alert_err", error=str(e))
