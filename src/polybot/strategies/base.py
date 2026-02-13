"""Base strategy interface and signal aggregation."""

from __future__ import annotations

from abc import ABC, abstractmethod

from polybot.data.models import MarketSnapshot, Signal


class BaseStrategy(ABC):
    """Abstract base class for all trading strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique strategy identifier."""
        ...

    @abstractmethod
    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        """Evaluate market data and optionally produce a trade signal."""
        ...

    @abstractmethod
    def get_params(self) -> dict:
        """Return current strategy parameters for logging/debugging."""
        ...
