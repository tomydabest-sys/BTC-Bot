"""Risk management — limits, sizing, exposure tracking.

PATCHED v4 (atop v2/v3 patches):
1. min_usd is now read from config (was: silent getattr default of 2.0)
2. can_open_position now uses projected exposure (current + sized notional),
   no longer rubber-stamps signals that would breach max_portfolio_exposure
3. can_place_order now also rejects when notional + portfolio exceeds cap,
   tightening the gate that previously had two competing checks
4. Exit orders bypass max_order_size + portfolio budget (kept from v2)
5. Defensive sizing floor — never returns 0 for a real signal (kept from v3)
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


def _is_exit_order(order: Order) -> bool:
    """Exit orders are tagged by main._execute_exit / auto_close paths."""
    s = (order.strategy or "").lower()
    return s.startswith("exit_") or s.startswith("auto_exit")


# Hardcoded fallback floor — only used if config.min_usd is somehow not set.
ABSOLUTE_MIN_USD_FLOOR = 2.0


class RiskManager:
    """Enforces risk limits and computes Kelly-aware position sizes."""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        self._daily_pnl_by_strategy: dict[str, float] = defaultdict(float)
        self._daily_reset_at: datetime = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        self._open_by_strategy: dict[str, float] = defaultdict(float)
        # Resolve min_usd: config field takes precedence, else hardcoded floor.
        min_usd = float(getattr(config, "min_usd", ABSOLUTE_MIN_USD_FLOOR))
        if min_usd <= 0:
            min_usd = ABSOLUTE_MIN_USD_FLOOR
        self._min_usd = min_usd
        self._sizing_policy = SizingPolicy(
            bankroll_usd=config.bankroll_usd,
            kelly_fraction=config.kelly_fraction,
            hard_cap_pct=config.hard_cap_pct,
            edge_floor_bps=config.edge_floor_bps,
            min_usd=min_usd,
            per_timeframe_cap_pct=dict(config.per_timeframe_cap_pct),
        )
        self._zero_size_counter = 0
        self._last_zero_warn_ts = 0.0
        # ATH-drawdown kill switch state. `_peak_equity` tracks the running
        # high-water mark; `_ath_killed` latches True permanently once tripped.
        self._peak_equity: float = float(config.bankroll_usd)
        self._ath_killed: bool = False

    @property
    def config(self) -> RiskConfig:
        return self._config

    @property
    def sizing_policy(self) -> SizingPolicy:
        return self._sizing_policy

    @property
    def min_usd(self) -> float:
        return self._min_usd

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
    #  ATH drawdown kill switch
    # ─────────────────────────────────────────────────────────────────

    def record_equity(self, equity_usd: float) -> None:
        """Update the high-water mark and trip the kill switch if we've
        fallen below the configured threshold.

        Calling code (Bot._status_log_loop or the maker orchestrator)
        should poke this every minute or so with portfolio.balance +
        unrealised P&L. A threshold of 0 disables the check entirely.
        """
        if equity_usd > self._peak_equity:
            self._peak_equity = equity_usd
        threshold = float(self._config.ath_drawdown_kill_pct)
        if threshold <= 0 or self._peak_equity <= 0:
            return
        drawdown = (self._peak_equity - equity_usd) / self._peak_equity
        if drawdown >= threshold and not self._ath_killed:
            self._ath_killed = True
            logger.critical(
                "ath_drawdown_kill",
                peak=round(self._peak_equity, 2),
                current=round(equity_usd, 2),
                drawdown_pct=round(drawdown * 100, 2),
                threshold_pct=round(threshold * 100, 2),
            )

    @property
    def ath_killed(self) -> bool:
        return self._ath_killed

    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    def reset_ath_kill(self) -> None:
        """Manual reset — operator only. Use when restarting after a kill."""
        self._ath_killed = False
        self._peak_equity = float(self._config.bankroll_usd)

    # ─────────────────────────────────────────────────────────────────
    #  Pre-trade gates (entry signals)
    # ─────────────────────────────────────────────────────────────────

    def can_open_position(
        self,
        portfolio: Portfolio,
        signal: Signal,
        timeframe: str = "",
        projected_notional: float = 0.0,
        projected_positions: int = 1,
    ) -> tuple[bool, str]:
        """Return (ok, reason). If projected_notional > 0, check it would not
        cause portfolio exposure to breach the cap.

        projected_positions is the number of *new* positions this signal will
        create. Defaults to 1; pass 2 for dual-direction arb (YES + NO legs)
        so the cap check accounts for both legs and we don't blow past
        max_positions in a single execution.
        """
        self._maybe_reset_daily()

        if self._ath_killed:
            self._emit_block(
                signal, BlockReason.KILL_SWITCH,
                f"ath_drawdown_kill peak=${self._peak_equity:.2f}",
            )
            return False, "ath_drawdown_kill"

        total_daily_pnl = sum(self._daily_pnl_by_strategy.values())
        if total_daily_pnl <= -abs(self._config.max_daily_loss):
            self._emit_block(signal, BlockReason.DAILY_LOSS_HALT,
                             f"daily_pnl ${total_daily_pnl:.2f}")
            return False, "daily_loss_halt"

        projected_count = len(portfolio.positions) + max(1, projected_positions)
        if projected_count > self._config.max_positions:
            self._emit_block(
                signal, BlockReason.POSITION_CAP,
                f"{len(portfolio.positions)}+{projected_positions}>"
                f"{self._config.max_positions}",
            )
            return False, "position_cap"

        # Projected exposure check — uses sized notional if caller passes it
        projected_total = portfolio.total_exposure + max(0.0, projected_notional)
        if projected_total > self._config.max_portfolio_exposure:
            self._emit_block(
                signal, BlockReason.RISK_BLOCK,
                f"projected exposure ${projected_total:.2f} > cap "
                f"${self._config.max_portfolio_exposure:.2f}",
            )
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
        meta = signal.metadata or {}
        fair_value = float(meta.get("fair_value", 0.0))
        edge_bps = float(meta.get("edge_bps", 0.0))

        # Legacy fallback path (no fair_value/edge_bps in metadata)
        if fair_value <= 0 or edge_bps <= 0:
            size = max(0.0, bankroll * signal.size_pct)
            if size > self._config.max_position_size:
                size = self._config.max_position_size
            if size <= 0 and signal.confidence > 0:
                size = self._min_usd
            return SizingResult(
                size_usd=size,
                kelly_f=signal.size_pct,
                capped_by="legacy_size_pct" if size > 0 else "legacy_floor",
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

        # Defensive floor — never return 0 for a real signal
        if result.size_usd <= 0.0:
            if edge_bps >= self._sizing_policy.edge_floor_bps:
                result.size_usd = self._min_usd
                result.capped_by = "floor_recovery"
                result.notes = (result.notes or "") + " | snapped from 0 to min_usd"
                self._zero_size_counter += 1
                now = time.time()
                if now - self._last_zero_warn_ts > 30:
                    self._last_zero_warn_ts = now
                    logger.warning(
                        "sizing_floor_recovery",
                        count_total=self._zero_size_counter,
                        edge_bps=edge_bps,
                        confidence=signal.confidence,
                        target=signal.target_price,
                        strategy=signal.strategy,
                        action="snapped_to_min_usd",
                        min_usd=self._min_usd,
                    )
        elif 0 < result.size_usd < self._min_usd:
            result.size_usd = self._min_usd
            result.capped_by = "min_floor"
            result.notes = (result.notes or "") + f" | floored to min_usd=${self._min_usd}"

        # Cap at max_position_size
        if result.size_usd > self._config.max_position_size:
            result.size_usd = float(self._config.max_position_size)
            result.capped_by = "max_position_size"

        logger.debug(
            "sizing_computed",
            strategy=signal.strategy,
            size_usd=round(result.size_usd, 2),
            capped_by=result.capped_by,
            edge_bps=round(edge_bps, 1),
            confidence=round(signal.confidence, 3),
            target=round(signal.target_price, 4),
        )

        return result

    # ─────────────────────────────────────────────────────────────────
    #  Order-level checks (place/execute path)
    # ─────────────────────────────────────────────────────────────────

    def can_place_order(
        self,
        order: Order,
        portfolio: Portfolio,
    ) -> tuple[bool, str]:
        """Pre-execution check.

        Exit orders bypass max_order_size + portfolio-budget gates because you
        must always be able to close an existing position. Entry orders go
        through the full risk gate including projected portfolio exposure.
        """
        notional = order.size * max(order.price, 0.01)
        is_exit = _is_exit_order(order)

        # Polymarket prices are bounded (0, 1). Reject anything outside this
        # range — either side — for both entry and exit orders. A price of
        # 1.29 or -0.05 cannot be filled on the real exchange and only appears
        # when a strategy's pricing math has gone off the rails (e.g.
        # unbounded inventory skew). Catching it here stops the paper engine
        # from simulating an impossible fill that immediately marks to market
        # at ~−95%.
        if not (0.0 < order.price < 1.0):
            return False, f"price_out_of_range:{order.price:.4f}"

        if is_exit:
            if notional <= 0:
                return False, "exit_zero_notional"
            return True, "ok_exit"

        # Entry orders: full risk gate
        if order.size <= 0 or notional <= 0:
            return False, "zero_size_or_notional"

        if notional > self._config.max_order_size:
            return False, (
                f"order_size ${notional:.2f} exceeds max_order_size "
                f"${self._config.max_order_size:.2f}"
            )

        if notional > self._config.max_position_size:
            return False, (
                f"order_size ${notional:.2f} exceeds max_position_size "
                f"${self._config.max_position_size:.2f}"
            )

        projected_total = portfolio.total_exposure + notional
        if projected_total > self._config.max_portfolio_exposure:
            return False, (
                f"projected exposure ${projected_total:.2f} > cap "
                f"${self._config.max_portfolio_exposure:.2f}"
            )

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
