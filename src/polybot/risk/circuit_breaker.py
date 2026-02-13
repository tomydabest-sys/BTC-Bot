"""Circuit breakers for automated risk control."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta

import structlog

from polybot.config import CircuitBreakerConfig

logger = structlog.get_logger()


class CircuitBreaker:
    """Monitors adverse conditions and halts trading when triggered."""

    def __init__(self, config: CircuitBreakerConfig) -> None:
        self._config = config
        self._consecutive_losses = 0
        self._api_errors: deque[datetime] = deque()
        self._paused_until: datetime | None = None
        self._size_multiplier = 1.0

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
                self._size_multiplier *= self._config.consecutive_losses_size_reduction
                logger.warning(
                    "circuit_breaker_size_reduction",
                    consecutive_losses=self._consecutive_losses,
                    new_multiplier=self._size_multiplier,
                )
        else:
            self._consecutive_losses = 0
            self._size_multiplier = min(self._size_multiplier * 1.1, 1.0)  # Gradual recovery

    def record_api_error(self) -> None:
        now = datetime.utcnow()
        self._api_errors.append(now)
        # Remove errors older than 1 minute
        cutoff = now - timedelta(minutes=1)
        while self._api_errors and self._api_errors[0] < cutoff:
            self._api_errors.popleft()

        if len(self._api_errors) >= self._config.api_errors_per_minute_pause:
            self._pause_trading(timedelta(minutes=5), "Too many API errors")

    def record_daily_loss_breach(self) -> None:
        # Pause for rest of day (until midnight UTC)
        now = datetime.utcnow()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        self._paused_until = tomorrow
        logger.critical("circuit_breaker_daily_halt", resume_at=tomorrow.isoformat())

    def _pause_trading(self, duration: timedelta, reason: str) -> None:
        self._paused_until = datetime.utcnow() + duration
        logger.warning(
            "circuit_breaker_pause",
            reason=reason,
            resume_at=self._paused_until.isoformat(),
        )
