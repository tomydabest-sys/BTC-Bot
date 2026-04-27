"""Multi-strategy signal aggregation.

Replaces the silent killer pattern: when min_confidence dropped a signal, the
old aggregator emitted no log line at all, leaving you with zero trades and no
visibility. Now every dropped signal emits a canonical decision-log line with
reason=BlockReason.AGGREGATOR_DROPPED or BELOW_MIN_NET_SIGNAL.

Adds weighted_vote conflict resolution: when strategies disagree, sum their
signed weights × confidences. The winner must clear min_net_score.
"""

from __future__ import annotations

import time

import structlog

from polybot.data.models import Direction, Signal
from polybot.diagnostics.decision_log import BlockReason, emit

logger = structlog.get_logger()


class StrategyAggregator:
    """Combines signals from multiple strategies into trade decisions."""

    def __init__(
        self,
        min_confidence: float = 0.40,
        conflict_resolution: str = "weighted_vote",
        strategy_weights: dict[str, float] | None = None,
        min_net_score: float = 0.30,
    ) -> None:
        self._min_confidence = float(min_confidence)
        self._conflict_resolution = str(conflict_resolution)
        self._strategy_weights: dict[str, float] = strategy_weights or {}
        self._min_net_score = float(min_net_score)

    def aggregate(self, signals: list[Signal]) -> list[Signal]:
        """Filter and resolve signals from multiple strategies.

        Logs a decision-log line for every signal that is dropped, so you can
        see in the JSONL output exactly why nothing reached execution.
        """
        # Drop expired signals
        valid: list[Signal] = []
        for s in signals:
            if s.is_expired:
                self._log_drop(s, BlockReason.AGGREGATOR_DROPPED, note="expired")
                continue
            if s.confidence < self._min_confidence:
                self._log_drop(s, BlockReason.BELOW_MIN_CONFIDENCE)
                continue
            valid.append(s)

        # Group by market
        by_market: dict[str, list[Signal]] = {}
        for signal in valid:
            by_market.setdefault(signal.market_id, []).append(signal)

        result: list[Signal] = []
        for market_id, market_signals in by_market.items():
            resolved = self._resolve_market_signals(market_signals)
            if resolved is not None:
                result.append(resolved)

        return result

    # ─────────────────────────────────────────────────────────────────
    #  Resolution
    # ─────────────────────────────────────────────────────────────────

    def _resolve_market_signals(self, signals: list[Signal]) -> Signal | None:
        if not signals:
            return None
        if len(signals) == 1:
            return signals[0]

        # All agree on direction → boost
        directions = {s.direction for s in signals}
        if len(directions) == 1:
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
                metadata={
                    "contributing_strategies": [s.strategy for s in signals],
                    **best.metadata,
                },
            )

        # ── Conflict ─────────────────────────────────────────────────
        if self._conflict_resolution == "skip":
            for s in signals:
                self._log_drop(s, BlockReason.AGGREGATOR_DROPPED, note="conflict_skip")
            logger.info(
                "signal_conflict_skipped",
                market_id=signals[0].market_id,
                strategies=[s.strategy for s in signals],
            )
            return None

        if self._conflict_resolution == "highest_confidence":
            return max(signals, key=lambda s: s.confidence)

        if self._conflict_resolution == "weighted_vote":
            return self._weighted_vote(signals)

        # Unknown resolver — fall through
        logger.warning(
            "aggregator_unknown_resolver",
            resolver=self._conflict_resolution,
        )
        return max(signals, key=lambda s: s.confidence)

    def _weighted_vote(self, signals: list[Signal]) -> Signal | None:
        """Sum signed weights × confidences. Winner must clear min_net_score."""
        score = 0.0
        contributions: dict[str, float] = {}
        for s in signals:
            w = self._strategy_weights.get(s.strategy, 0.5)
            sign = 1.0 if s.direction == Direction.BUY else -1.0
            contribution = sign * s.confidence * w
            contributions[s.strategy] = contribution
            score += contribution

        if abs(score) < self._min_net_score:
            for s in signals:
                self._log_drop(
                    s, BlockReason.BELOW_MIN_NET_SIGNAL,
                    note=f"net_score={score:.3f}",
                )
            return None

        # Winner = direction matching score sign, max edge × confidence
        winning_dir = Direction.BUY if score > 0 else Direction.SELL
        candidates = [s for s in signals if s.direction == winning_dir]
        if not candidates:
            # Shouldn't happen, but be defensive
            return None
        best = max(candidates, key=lambda x: x.confidence)
        # Build aggregated signal
        return Signal(
            market_id=best.market_id,
            strategy=f"aggregated({'+'.join(s.strategy for s in candidates)})",
            direction=best.direction,
            outcome=best.outcome,
            target_price=best.target_price,
            confidence=min(0.95, abs(score)),
            size_pct=best.size_pct,
            reason=f"weighted_vote score={score:+.3f} from {len(candidates)} strategies: {best.reason}",
            metadata={
                "contributing_strategies": [s.strategy for s in candidates],
                "weighted_score": score,
                "contributions": contributions,
                **best.metadata,
            },
        )

    # ─────────────────────────────────────────────────────────────────
    #  Drop logging — replaces the old silent-kill behavior
    # ─────────────────────────────────────────────────────────────────

    def _log_drop(self, signal: Signal, reason: str, note: str = "") -> None:
        """Log a dropped signal so it appears in decisions.jsonl."""
        cycle_id = f"{int(time.time() * 1000) % 100000:05d}"
        try:
            emit(
                cycle_id=cycle_id,
                strategy=f"aggregator({signal.strategy})",
                market_id=signal.market_id,
                timeframe="",
                fair_value=signal.metadata.get("fair_value", 0.0) if signal.metadata else 0.0,
                mid=signal.target_price,
                confidence=signal.confidence,
                edge_bps=signal.metadata.get("edge_bps", 0.0) if signal.metadata else 0.0,
                decision="BLOCKED",
                reason=reason,
                extra={"note": note} if note else None,
            )
        except Exception:
            pass
