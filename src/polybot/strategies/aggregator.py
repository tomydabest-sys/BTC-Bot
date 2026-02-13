"""Multi-strategy signal aggregation."""

from __future__ import annotations

import structlog

from polybot.data.models import Signal

logger = structlog.get_logger()


class StrategyAggregator:
    """
    Combines signals from multiple strategies.
    Handles conflicts and produces final trade decisions.
    """

    def __init__(
        self,
        min_confidence: float = 0.5,
        conflict_resolution: str = "skip",
    ) -> None:
        self._min_confidence = min_confidence
        self._conflict_resolution = conflict_resolution

    def aggregate(self, signals: list[Signal]) -> list[Signal]:
        """Filter and resolve signals from multiple strategies."""
        # Remove expired and low-confidence signals
        valid = [s for s in signals if not s.is_expired and s.confidence >= self._min_confidence]

        # Group by market
        by_market: dict[str, list[Signal]] = {}
        for signal in valid:
            by_market.setdefault(signal.market_id, []).append(signal)

        result: list[Signal] = []
        for market_id, market_signals in by_market.items():
            resolved = self._resolve_market_signals(market_signals)
            if resolved:
                result.append(resolved)

        return result

    def _resolve_market_signals(self, signals: list[Signal]) -> Signal | None:
        if len(signals) == 1:
            return signals[0]

        # Check for agreement
        directions = {s.direction for s in signals}
        if len(directions) == 1:
            # All agree — boost confidence of the highest-confidence signal
            best = max(signals, key=lambda s: s.confidence)
            boosted_confidence = min(best.confidence * 1.2, 1.0)
            return Signal(
                market_id=best.market_id,
                strategy=f"aggregated({','.join(s.strategy for s in signals)})",
                direction=best.direction,
                outcome=best.outcome,
                target_price=best.target_price,
                confidence=boosted_confidence,
                size_pct=best.size_pct,
                reason=f"Confirmed by {len(signals)} strategies: {best.reason}",
                metadata={"contributing_strategies": [s.strategy for s in signals]},
            )

        # Conflict
        if self._conflict_resolution == "skip":
            logger.info(
                "signal_conflict_skipped",
                market_id=signals[0].market_id,
                strategies=[s.strategy for s in signals],
            )
            return None

        if self._conflict_resolution == "highest_confidence":
            return max(signals, key=lambda s: s.confidence)

        return None
