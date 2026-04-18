"""Position tracking and P&L management.

Adds strategy-aware exit logic for `overshoot_reversion` (take-profit on small
retrace + time-based timeout). Existing stop-loss behaviour preserved.
"""

from __future__ import annotations

from datetime import datetime

import structlog

from polybot.data.models import (
    ExitSignal,
    Order,
    OrderStatus,
    Portfolio,
    Position,
    Side,
)
from polybot.events import EventBus

logger = structlog.get_logger()


# Overshoot reversion exit parameters
OVERSHOOT_TP_MOVE = 0.012          # Entry-direction mid move >= 1.2c → take profit
OVERSHOOT_TIMEOUT_SECONDS = 120.0  # Hard timeout
OVERSHOOT_SL_MOVE = 0.030          # Against-us mid move >= 3c → cut early (ahead of PnL stop)


class PositionManager:
    """Tracks open positions and computes P&L."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._positions: dict[str, Position] = {}
        self._realized_pnl = 0.0
        self._daily_pnl = 0.0
        self._daily_reset_date: str = ""

    def get_portfolio(self) -> Portfolio:
        self._maybe_reset_daily()
        return Portfolio(
            positions=list(self._positions.values()),
            realized_pnl=self._realized_pnl,
            daily_pnl=self._daily_pnl,
        )

    def update_from_fill(self, order: Order) -> None:
        if order.status != OrderStatus.FILLED:
            return

        market_id = order.market_id
        existing = self._positions.get(market_id)

        if existing and existing.side != order.side:
            close_size = min(existing.size, order.filled_size)
            if order.side == Side.SELL:
                pnl = close_size * (order.avg_fill_price - existing.avg_entry_price)
            else:
                pnl = close_size * (existing.avg_entry_price - order.avg_fill_price)

            self._realized_pnl += pnl
            self._daily_pnl += pnl

            existing.size -= close_size
            if existing.size <= 0.001:
                del self._positions[market_id]
                logger.info(
                    "position_closed",
                    market=market_id,
                    pnl=pnl,
                    strategy=existing.strategy,
                )
            else:
                logger.info(
                    "position_reduced",
                    market=market_id,
                    remaining=existing.size,
                    pnl=pnl,
                )
        elif existing and existing.side == order.side:
            total_cost = (existing.size * existing.avg_entry_price) + (
                order.filled_size * order.avg_fill_price
            )
            existing.size += order.filled_size
            existing.avg_entry_price = total_cost / existing.size
            logger.info(
                "position_increased",
                market=market_id,
                new_size=existing.size,
                avg_entry=existing.avg_entry_price,
            )
        else:
            self._positions[market_id] = Position(
                market_id=market_id,
                token_id=order.token_id,
                outcome="Yes" if order.side == Side.BUY else "No",
                side=order.side,
                size=order.filled_size,
                avg_entry_price=order.avg_fill_price,
                strategy=order.strategy,
            )
            logger.info(
                "position_opened",
                market=market_id,
                side=order.side,
                size=order.filled_size,
                price=order.avg_fill_price,
                strategy=order.strategy,
            )

    def update_prices(self, market_id: str, current_price: float) -> None:
        if market_id in self._positions:
            self._positions[market_id].current_price = current_price

    def check_exits(self, stop_loss_pct: float = 0.05) -> list[ExitSignal]:
        """Check stop-loss + strategy-specific exit conditions."""
        exits: list[ExitSignal] = []
        now = datetime.utcnow()

        for pos in self._positions.values():
            if pos.current_price == 0:
                continue

            # ── Universal stop-loss ──────────────────────────────
            if pos.notional > 0:
                loss_pct = -pos.unrealized_pnl / pos.notional
                if loss_pct > stop_loss_pct:
                    exits.append(
                        ExitSignal(
                            position=pos,
                            reason=f"Stop-loss triggered: {loss_pct:.1%} loss",
                            urgency="immediate",
                        )
                    )
                    continue

            # ── Overshoot reversion strategy-specific exits ──────
            strategy = (pos.strategy or "").lower()
            if strategy.startswith("overshoot_reversion"):
                exit_signal = self._check_overshoot_exit(pos, now)
                if exit_signal is not None:
                    exits.append(exit_signal)
                    continue

        return exits

    def _check_overshoot_exit(
        self, pos: Position, now: datetime
    ) -> ExitSignal | None:
        """Strategy-specific exit for overshoot_reversion positions."""
        opened_at = pos.opened_at
        if getattr(opened_at, "tzinfo", None) is not None:
            opened_at = opened_at.replace(tzinfo=None)
        age = (now - opened_at).total_seconds()

        direction_sign = 1.0 if pos.side == Side.BUY else -1.0
        mid_move = (pos.current_price - pos.avg_entry_price) * direction_sign

        # Take profit — reverted in our favour
        if mid_move >= OVERSHOOT_TP_MOVE:
            return ExitSignal(
                position=pos,
                reason=f"overshoot_tp mid_move={mid_move:+.4f}",
                urgency="normal",
            )

        # Adverse move — cut before the 5% PnL stop triggers
        if mid_move <= -OVERSHOOT_SL_MOVE:
            return ExitSignal(
                position=pos,
                reason=f"overshoot_sl mid_move={mid_move:+.4f}",
                urgency="immediate",
            )

        # Timeout
        if age >= OVERSHOOT_TIMEOUT_SECONDS:
            return ExitSignal(
                position=pos,
                reason=f"overshoot_timeout age={age:.0f}s mid_move={mid_move:+.4f}",
                urgency="normal",
            )

        return None

    def _maybe_reset_daily(self) -> None:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            self._daily_pnl = 0.0
            self._daily_reset_date = today
