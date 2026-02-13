"""Order execution engine — manages order lifecycle on Polymarket CLOB."""

from __future__ import annotations

import structlog

from polybot.config import ExecutionConfig
from polybot.data.client import PolymarketClient
from polybot.data.models import Order, OrderStatus, RiskDecision
from polybot.events import EventBus
from polybot.risk.manager import RiskManager
from polybot.data.models import Portfolio

logger = structlog.get_logger()


class ExecutionEngine:
    """Submits orders and manages their lifecycle."""

    def __init__(
        self,
        client: PolymarketClient,
        risk_manager: RiskManager,
        config: ExecutionConfig,
        event_bus: EventBus,
        is_paper: bool = True,
    ) -> None:
        self._client = client
        self._risk_manager = risk_manager
        self._config = config
        self._event_bus = event_bus
        self._is_paper = is_paper
        self._open_orders: dict[str, Order] = {}

    async def execute_order(self, order: Order, portfolio: Portfolio) -> Order:
        """Submit an order after risk checks."""
        # Risk check
        risk_result = self._risk_manager.check_order(order, portfolio)

        if risk_result.decision == RiskDecision.REJECT:
            order.status = OrderStatus.REJECTED
            logger.info("order_rejected", reason=risk_result.reason, market=order.market_id)
            await self._event_bus.emit("order_rejected", order=order, reason=risk_result.reason)
            return order

        if risk_result.decision == RiskDecision.REDUCE and risk_result.modified_size:
            order.size = risk_result.modified_size
            logger.info("order_size_reduced", new_size=order.size, reason=risk_result.reason)

        if self._is_paper:
            return await self._paper_execute(order)

        return await self._live_execute(order)

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if self._is_paper:
            if order_id in self._open_orders:
                self._open_orders[order_id].status = OrderStatus.CANCELLED
                del self._open_orders[order_id]
                return True
            return False
        return await self._client.cancel_order(order_id)

    async def cancel_all(self) -> int:
        """Cancel all open orders. Returns count cancelled."""
        cancelled = 0
        for order_id in list(self._open_orders):
            if await self.cancel_order(order_id):
                cancelled += 1
        return cancelled

    async def _paper_execute(self, order: Order) -> Order:
        """Simulate order execution for paper trading."""
        order.status = OrderStatus.FILLED
        order.filled_size = order.size
        order.avg_fill_price = order.price
        order.order_id = f"paper_{order.market_id}_{order.created_at.timestamp()}"

        logger.info(
            "paper_order_filled",
            order_id=order.order_id,
            market=order.market_id,
            side=order.side,
            price=order.price,
            size=order.size,
            strategy=order.strategy,
        )

        self._risk_manager.record_trade(order.market_id)
        await self._event_bus.emit("order_filled", order=order)
        return order

    async def _live_execute(self, order: Order) -> Order:
        """Submit order to Polymarket CLOB API."""
        payload = {
            "token_id": order.token_id,
            "side": order.side.value,
            "price": str(order.price),
            "size": str(order.size),
            "type": order.order_type.value,
        }

        for attempt in range(self._config.retry_attempts):
            try:
                result = await self._client.place_order(payload)
                order.order_id = result.get("id", result.get("order_id", ""))
                order.status = OrderStatus.OPEN
                self._open_orders[order.order_id] = order
                logger.info(
                    "order_submitted",
                    order_id=order.order_id,
                    market=order.market_id,
                    side=order.side,
                    price=order.price,
                    size=order.size,
                )
                self._risk_manager.record_trade(order.market_id)
                await self._event_bus.emit("order_submitted", order=order)
                return order
            except Exception as e:
                backoff = self._config.retry_backoff_seconds
                wait = backoff[attempt] if attempt < len(backoff) else backoff[-1]
                logger.warning(
                    "order_submit_retry",
                    attempt=attempt + 1,
                    error=str(e),
                    wait=wait,
                )
                import asyncio
                await asyncio.sleep(wait)

        order.status = OrderStatus.REJECTED
        logger.error("order_submit_failed", market=order.market_id)
        await self._event_bus.emit("order_rejected", order=order, reason="Max retries exceeded")
        return order
