"""Risk management — limits, sizing, exposure tracking.

Major changes vs original:
1. Adds `kelly_size_for_signal()` — Quarter-Kelly with confidence + per-tf caps
2. Decision-log emits on every gate failure (no more silent risk blocks)
3. Per-strategy daily-loss tracking
4. Configurable edge floor before any sizing happens
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from polybot.config import RiskConfig
from polybot.data.models import Order, Portfolio, Signal
from polybot.diagnostics.decision_log import BlockReason, emit
from polybot.risk.sizing import (
    SizingPolicy,
    SizingResult,
    derive_p_win_from_signal,
    position_size,
)

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()


class RiskManager:
    """Enforces risk limits and computes Kelly-aware position sizes."""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        # Per-strategy daily P&L tracking
        self._daily_pnl_by_strategy: dict[str, float] = defaultdict(float)
        self._daily_reset_at: datetime = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        # Per-strategy open exposure
        self._open_by_strategy: dict[str, float] = defaultdict(float)
        # Sizing policy (built once from config)
        self._sizing_policy = SizingPolicy(
            bankroll_usd=config.bankroll_usd,
            kelly_fraction=config.kelly_fraction,
            hard_cap_pct=config.hard_cap_pct,
            edge_floor_bps=config.edge_floor_bps,
            min_usd=5.0,
            per_timeframe_cap_pct=dict(config.per_timeframe_cap_pct),
        )

    @property
    def config(self) -> RiskConfig:
        return self._config

    @property
    def sizing_policy(self) -> SizingPolicy:
        return self._sizing_policy

    # ─────────────────────────────────────────────────────────────────
    #  Daily reset
    # ─────────────────────────────────────────────────────────────────

    def _maybe_reset_daily(self) -> None:
        now = datetime.utcnow()
        if now >= self._daily_reset_at:
            self._daily_pnl_by_strategy.clear()
            self._daily_reset_at = now.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)
            logger.info("risk_daily_reset", at=self._daily_reset_at.isoformat())

    def record_pnl(self, strategy: str, pnl: float) -> None:
        self._maybe_reset_daily()
        self._daily_pnl_by_strategy[strategy] += pnl

    def record_open_exposure(self, strategy: str, delta_usd: float) -> None:
        self._open_by_strategy[strategy] += delta_usd
        if self._open_by_strategy[strategy] < 0:
            self._open_by_strategy[strategy] = 0.0

    # ─────────────────────────────────────────────────────────────────
    #  Pre-trade gates
    # ─────────────────────────────────────────────────────────────────

    def can_open_position(
        self,
        portfolio: Portfolio,
        signal: Signal,
        timeframe: str = "",
    ) -> tuple[bool, str]:
        """Check whether opening this position passes all risk gates.

        Returns (allowed, reason). On rejection, also emits a decision-log line.
        """
        self._maybe_reset_daily()

        # Daily loss kill-switch
        total_daily_pnl = sum(self._daily_pnl_by_strategy.values())
        if total_daily_pnl <= -abs(self._config.max_daily_loss):
            self._emit_block(signal, BlockReason.DAILY_LOSS_HALT,
                             f"daily_pnl ${total_daily_pnl:.2f}")
            return False, "daily_loss_halt"

        # Concurrent-position cap
        if len(portfolio.positions) >= self._config.max_positions:
            self._emit_block(signal, BlockReason.POSITION_CAP,
                             f"{len(portfolio.positions)}/{self._config.max_positions} open")
            return False, "position_cap"

        # Total portfolio exposure
        if portfolio.total_exposure >= self._config.max_portfolio_exposure:
            self._emit_block(signal, BlockReason.RISK_BLOCK,
                             f"exposure ${portfolio.total_exposure:.2f}")
            return False, "portfolio_exposure"

        return True, "ok"

    # ─────────────────────────────────────────────────────────────────
    #  Sizing
    # ─────────────────────────────────────────────────────────────────

    def kelly_size_for_signal(
        self,
        signal: Signal,
        bankroll: float,
        timeframe: str = "",
    ) -> SizingResult:
        """Quarter-Kelly size for a signal, with confidence + tf caps applied.

        Pulls fair_value and edge_bps from signal.metadata. If those are
        missing, falls back to flat `bankroll * size_pct`.
        """
        meta = signal.metadata or {}
        fair_value = float(meta.get("fair_value", 0.0))
        edge_bps = float(meta.get("edge_bps", 0.0))

        # Fallback path: no fair value → use legacy size_pct
        if fair_value <= 0 or edge_bps <= 0:
            size = max(0.0, bankroll * signal.size_pct)
            if size > self._config.max_position_size:
                size = self._config.max_position_size
            return SizingResult(
                size_usd=size,
                kelly_f=signal.size_pct,
                capped_by="legacy_size_pct",
                notes="no fair_value/edge_bps in signal.metadata",
            )

        is_buy = signal.direction.value == "BUY"
        p_win, avg_win, avg_loss = derive_p_win_from_signal(
            target_price=signal.target_price,
            fair_value=fair_value,
            direction_buy=is_buy,
        )

        result = position_size(
            bankroll=bankroll,
            p_win=p_win,
            avg_win=avg_win,
            avg_loss=avg_loss,
            edge_bps=edge_bps,
            confidence=signal.confidence,
            timeframe=timeframe,
            policy=self._sizing_policy,
        )

        # Apply absolute hard cap from config
        if result.size_usd > self._config.max_position_size:
            result.size_usd = float(self._config.max_position_size)
            result.capped_by = "max_position_size"

        return result

    # ─────────────────────────────────────────────────────────────────
    #  Order-level checks
    # ─────────────────────────────────────────────────────────────────

    def can_place_order(
        self,
        order: Order,
        portfolio: Portfolio,
    ) -> tuple[bool, str]:
        notional = order.size * max(order.price, 0.01)

        if notional > self._config.max_order_size and notional > self._config.max_position_size:
            return False, f"order_size ${notional:.2f} exceeds max"

        # Need to fit within remaining portfolio budget
        remaining = self._config.max_portfolio_exposure - portfolio.total_exposure
        if notional > remaining:
            return False, f"insufficient_portfolio_budget (rem=${remaining:.2f})"

        return True, "ok"

    # ─────────────────────────────────────────────────────────────────
    #  Internals
    # ─────────────────────────────────────────────────────────────────

    def _emit_block(self, signal: Signal, reason: str, note: str) -> None:
        try:
            cycle_id = f"{int(time.time() * 1000) % 100000:05d}"
            emit(
                cycle_id=cycle_id,
                strategy=f"risk({signal.strategy})",
                market_id=signal.market_id,
                mid=signal.target_price,
                confidence=signal.confidence,
                edge_bps=(signal.metadata or {}).get("edge_bps", 0.0),
                decision="BLOCKED",
                reason=reason,
                extra={"note": note},
            )
        except Exception:
            pass
