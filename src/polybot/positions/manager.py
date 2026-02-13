"""Position tracking and P&L management."""

from __future__ import annotations

from datetime import datetime

import structlog

from polybot.data.models import (
    ExitSignal,
    Fill,
    Order,
    OrderStatus,
    Portfolio,
    Position,
    Side,
)
from polybot.events import EventBus

logger = structlog.get_logger()


class PositionManager:
    """Tracks open positions and computes P&L."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._positions: dict[str, Position] = {}  # keyed by market_id
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
        """Update positions from a filled order."""
        if order.status != OrderStatus.FILLED:
            return

        market_id = order.market_id
        existing = self._positions.get(market_id)

        if existing and existing.side != order.side:
            # Closing or reducing position
            close_size = min(existing.size, order.filled_size)
            if order.side == Side.SELL:
                pnl = close_size * (order.avg_fill_price - existing.avg_entry_price)
            else:
                pnl = close_size * (existing.avg_entry_price - order.avg_fill_price)

            self._realized_pnl += pnl
            self._daily_pnl += pnl

            existing.size -= close_size
            if existing.size <= 0.001:  # Effectively closed
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
            # Adding to position — update average entry
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
            # New position
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
        """Update mark-to-market price for a position."""
        if market_id in self._positions:
            self._positions[market_id].current_price = current_price

    def check_exits(self, stop_loss_pct: float = 0.05) -> list[ExitSignal]:
        """Check for stop-loss conditions across all positions."""
        exits: list[ExitSignal] = []
        for pos in self._positions.values():
            if pos.current_price == 0:
                continue
            loss_pct = -pos.unrealized_pnl / pos.notional if pos.notional > 0 else 0
            if loss_pct > stop_loss_pct:
                exits.append(
                    ExitSignal(
                        position=pos,
                        reason=f"Stop-loss triggered: {loss_pct:.1%} loss",
                        urgency="immediate",
                    )
                )
        return exits

    def _maybe_reset_daily(self) -> None:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            self._daily_pnl = 0.0
            self._daily_reset_date = today
