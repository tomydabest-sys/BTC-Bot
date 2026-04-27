"""Position tracking and exit logic.

Strategy-specific exit rules:
- overshoot_reversion: TP at 1.2¢ favourable move OR 120s timeout OR 2¢ stop
- boundary_decay     : HOLD TO EXPIRY (auto-close handles <20s window)
                        — boundary trades are near-certainty; don't TP early
- dual_direction_arb : Both legs held to expiry (one always pays $1)
- maker_edge         : Inventory neutralisation by re-quote, no time exit here
- Default fallback   : 5% stop, 10% TP, 5-minute timeout

This rewrite adds an explicit dispatch by strategy name, so adding a new
strategy means adding a `_exits_for_<name>()` method, not editing the trunk.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from polybot.data.models import (
    Order,
    Portfolio,
    Position,
    PositionStatus,
    Side,
)
from polybot.events import EventBus

logger = structlog.get_logger()


@dataclass
class ExitSignal:
    position: Position
    reason: str


class PositionManager:
    """Tracks open positions, computes unrealised P&L, generates exit signals."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._portfolio = Portfolio()
        self._position_open_ts: dict[str, float] = {}

    @property
    def portfolio(self) -> Portfolio:
        return self._portfolio

    def get_portfolio(self) -> Portfolio:
        return self._portfolio

    # ─────────────────────────────────────────────────────────────────
    #  Fill handling
    # ─────────────────────────────────────────────────────────────────

    def update_from_fill(self, order: Order) -> None:
        """Update positions from a filled order.

        Resilient to partial fills, re-fills, and exit orders (whose strategy
        name starts with 'exit_' or 'auto_exit').
        """
        if order.filled_size <= 0:
            return

        is_exit = order.strategy.startswith("exit_") or order.strategy.startswith("auto_exit")

        # Find existing position by (market_id, token_id, side)
        # Exit orders have side opposite to position side
        target_side = order.side
        if is_exit:
            target_side = Side.SELL if order.side == Side.BUY else Side.BUY

        existing = None
        for p in self._portfolio.positions:
            if p.market_id == order.market_id and p.token_id == order.token_id and p.side == target_side:
                existing = p
                break

        if is_exit and existing is not None:
            # Close (or reduce) the position
            close_size = min(order.filled_size, existing.size)
            pnl = self._compute_realised_pnl(existing, order.avg_fill_price, close_size)
            self._portfolio.realized_pnl += pnl
            self._portfolio.daily_pnl += pnl
            existing.size -= close_size
            if existing.size <= 1e-9:
                existing.status = PositionStatus.CLOSED
                self._portfolio.positions.remove(existing)
                self._position_open_ts.pop(_pos_key(existing), None)
                logger.info(
                    "position_closed",
                    m=existing.market_id[:12],
                    pnl=round(pnl, 4),
                    strat=existing.strategy,
                )
            else:
                logger.info(
                    "position_reduced",
                    m=existing.market_id[:12],
                    new_size=round(existing.size, 4),
                    pnl=round(pnl, 4),
                )
            return

        # Open or add to position
        if existing is None:
            pos = Position(
                market_id=order.market_id,
                token_id=order.token_id,
                side=order.side,
                size=order.filled_size,
                avg_entry_price=order.avg_fill_price,
                current_price=order.avg_fill_price,
                strategy=order.strategy,
                status=PositionStatus.OPEN,
                opened_at=datetime.utcnow(),
            )
            self._portfolio.positions.append(pos)
            self._position_open_ts[_pos_key(pos)] = time.time()
            logger.info(
                "position_opened",
                m=pos.market_id[:12],
                side=pos.side.value,
                px=round(pos.avg_entry_price, 4),
                sz=round(pos.size, 4),
                strat=pos.strategy,
            )
        else:
            # Add to existing — recompute weighted avg entry
            total_size = existing.size + order.filled_size
            if total_size > 0:
                existing.avg_entry_price = (
                    existing.avg_entry_price * existing.size
                    + order.avg_fill_price * order.filled_size
                ) / total_size
                existing.size = total_size
                logger.info(
                    "position_increased",
                    m=existing.market_id[:12],
                    new_size=round(existing.size, 4),
                    new_avg=round(existing.avg_entry_price, 4),
                )

    def update_prices(self, market_id: str, current_price: float) -> None:
        """Mark-to-market all positions in this market."""
        for p in self._portfolio.positions:
            if p.market_id == market_id:
                p.current_price = current_price
                p.unrealized_pnl = self._compute_unrealised_pnl(p)

    # ─────────────────────────────────────────────────────────────────
    #  Exit logic
    # ─────────────────────────────────────────────────────────────────

    def check_exits(self) -> list[ExitSignal]:
        """Return exit signals for any open position whose exit rule fires."""
        out: list[ExitSignal] = []
        for p in list(self._portfolio.positions):
            if p.status != PositionStatus.OPEN:
                continue
            try:
                exit_reason = self._exit_reason_for(p)
            except Exception as e:
                logger.warning(
                    "exit_check_err",
                    m=p.market_id[:12],
                    strat=p.strategy,
                    error=str(e),
                )
                continue
            if exit_reason:
                out.append(ExitSignal(position=p, reason=exit_reason))
        return out

    def _exit_reason_for(self, p: Position) -> str | None:
        """Dispatch to strategy-specific exit logic."""
        strat = (p.strategy or "").lower()

        if "boundary_decay" in strat:
            return self._exits_for_boundary_decay(p)
        if "dual_direction" in strat:
            return self._exits_for_dual_direction(p)
        if "overshoot_reversion" in strat:
            return self._exits_for_overshoot_reversion(p)
        if "maker_edge" in strat:
            return self._exits_for_maker_edge(p)

        return self._exits_for_default(p)

    # ─── Strategy-specific exit rules ────────────────────────────────

    def _exits_for_boundary_decay(self, p: Position) -> str | None:
        """Boundary decay positions: HOLD TO EXPIRY.

        These trades are near-certainty terminal payoffs. The expected value
        of holding to resolution exceeds any TP target the position might
        cross during the final minute. The auto-close logic in main._trading_loop
        handles closing within 20s of expiry.

        We only exit early on a hard catastrophic stop (≥10% adverse), which
        signals the BTC underlying has reversed through the strike.
        """
        unrealised_pct = self._unrealised_pct(p)
        if unrealised_pct <= -0.10:
            return f"boundary_hard_stop ({unrealised_pct:.2%})"
        return None

    def _exits_for_dual_direction(self, p: Position) -> str | None:
        """Dual-direction arb: both legs held to expiry. No early exit."""
        return None

    def _exits_for_overshoot_reversion(self, p: Position) -> str | None:
        """Overshoot reversion: TP on reversion, timeout, hard stop."""
        # Hard stop
        if p.unrealized_pnl <= -0.02 * (p.avg_entry_price * p.size):
            return f"overshoot_hard_stop ({p.unrealized_pnl:.4f})"

        # TP at 1.2¢ favourable price move
        if p.side == Side.BUY:
            move = p.current_price - p.avg_entry_price
        else:
            move = p.avg_entry_price - p.current_price
        if move >= 0.012:
            return f"overshoot_tp ({move:+.4f})"

        # Timeout
        opened = self._position_open_ts.get(_pos_key(p), 0.0)
        if opened > 0 and (time.time() - opened) >= 120:
            return "overshoot_timeout (120s)"

        return None

    def _exits_for_maker_edge(self, p: Position) -> str | None:
        """Maker positions: rebalance via re-quote, not via this path.

        We only exit on extreme adverse selection or end-of-window.
        """
        unrealised_pct = self._unrealised_pct(p)
        if unrealised_pct <= -0.05:
            return f"maker_adverse_selection ({unrealised_pct:.2%})"
        return None

    def _exits_for_default(self, p: Position) -> str | None:
        """Default fallback for any strategy not explicitly handled."""
        unrealised_pct = self._unrealised_pct(p)
        if unrealised_pct <= -0.05:
            return f"default_stop ({unrealised_pct:.2%})"
        if unrealised_pct >= 0.10:
            return f"default_tp ({unrealised_pct:.2%})"
        opened = self._position_open_ts.get(_pos_key(p), 0.0)
        if opened > 0 and (time.time() - opened) >= 300:
            return "default_timeout (5min)"
        return None

    # ─────────────────────────────────────────────────────────────────
    #  P&L math
    # ─────────────────────────────────────────────────────────────────

    def _compute_realised_pnl(
        self, position: Position, exit_price: float, close_size: float,
    ) -> float:
        if position.side == Side.BUY:
            return (exit_price - position.avg_entry_price) * close_size
        else:
            return (position.avg_entry_price - exit_price) * close_size

    def _compute_unrealised_pnl(self, p: Position) -> float:
        if p.side == Side.BUY:
            return (p.current_price - p.avg_entry_price) * p.size
        else:
            return (p.avg_entry_price - p.current_price) * p.size

    def _unrealised_pct(self, p: Position) -> float:
        notional = p.avg_entry_price * p.size
        if notional <= 0:
            return 0.0
        return p.unrealized_pnl / notional


def _pos_key(p: Position) -> str:
    return f"{p.market_id}:{p.token_id}:{p.side.value}"
