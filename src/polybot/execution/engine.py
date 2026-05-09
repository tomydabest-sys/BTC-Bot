"""Order execution — paper and live.

PATCHED v3:
1. Reads dual-direction metadata from order.metadata (Order now has the field)
2. Retry/backoff for live order placement (config.execution.retry_attempts)
3. Live order placement is hard-guarded behind LIVE_TRADING_ENABLED in client.py
   (raises LiveTradingNotImplementedError clearly)
4. Cancel calls now pass market_id (matches client's new signature)
5. Emits decision-log lines for execution-stage rejections
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime

import structlog

from polybot.config import ExecutionConfig
from polybot.data.client import (
    LiveTradingNotImplementedError,
    PolymarketClient,
)
from polybot.data.models import (
    Order,
    OrderStatus,
    Portfolio,
    Side,
)
from polybot.diagnostics.decision_log import BlockReason, emit
from polybot.events import EventBus
from polybot.risk.manager import RiskManager

logger = structlog.get_logger()


class ExecutionEngine:
    """Places orders, manages fills, handles dual-leg arb atomicity."""

    def __init__(
        self,
        client: PolymarketClient,
        risk_manager: RiskManager,
        config: ExecutionConfig,
        event_bus: EventBus,
        is_paper: bool = True,
    ) -> None:
        self._client = client
        self._risk = risk_manager
        self._config = config
        self._event_bus = event_bus
        self._is_paper = is_paper
        self._last_request_ts: list[float] = []
        self._open_orders: dict[str, Order] = {}

    @property
    def is_paper(self) -> bool:
        return self._is_paper

    # ─────────────────────────────────────────────────────────────────
    #  Public API
    # ─────────────────────────────────────────────────────────────────

    async def execute_order(self, order: Order, portfolio: Portfolio) -> Order:
        """Place an order. For dual-direction arb signals, places both legs."""
        ok, reason = self._risk.can_place_order(order, portfolio)
        if not ok:
            logger.warning(
                "exec_blocked",
                m=order.market_id[:12],
                strat=order.strategy,
                reason=reason,
            )
            self._emit_exec_block(order, BlockReason.RISK_BLOCK, reason)
            order.status = OrderStatus.CANCELLED
            return order

        meta = order.metadata or {}
        is_dual = bool(meta.get("is_dual_direction"))

        if is_dual:
            return await self._execute_dual_leg(order, portfolio, meta)

        await self._rate_limit()

        if self._is_paper:
            return await self._simulate_paper_fill(order)

        return await self._submit_live_with_retry(order)

    async def cancel_all(self) -> None:
        if self._is_paper:
            self._open_orders.clear()
            return
        for order_id, order in list(self._open_orders.items()):
            try:
                await self._client.cancel_order(order_id, market_id=order.market_id)
            except Exception as e:
                logger.warning(
                    "cancel_err",
                    order_id=order_id[:16],
                    error=str(e),
                )
        self._open_orders.clear()

    # ─────────────────────────────────────────────────────────────────
    #  Live submission with retry
    # ─────────────────────────────────────────────────────────────────

    async def _submit_live_with_retry(self, order: Order) -> Order:
        """Place a live order with config-driven retry/backoff.

        Raises LiveTradingNotImplementedError immediately rather than retrying
        — that's a permanent error pending implementation.
        """
        attempts = max(1, int(self._config.retry_attempts))
        backoffs = list(self._config.retry_backoff_seconds) or [1.0]
        last_err: Exception | None = None

        for attempt in range(attempts):
            try:
                placed = await self._client.place_order(order)
                self._open_orders[placed.order_id] = placed
                return placed
            except LiveTradingNotImplementedError:
                # Permanent error — don't retry, surface clearly
                logger.error(
                    "place_order_live_not_implemented",
                    m=order.market_id[:12],
                    note="set bot.mode='paper' or implement EIP-712 signing",
                )
                self._emit_exec_block(
                    order, BlockReason.NO_FEED,
                    "live_trading_not_implemented",
                )
                order.status = OrderStatus.REJECTED
                return order
            except Exception as e:
                last_err = e
                if attempt < attempts - 1:
                    sleep_s = backoffs[min(attempt, len(backoffs) - 1)]
                    logger.warning(
                        "place_order_retry",
                        m=order.market_id[:12],
                        attempt=attempt + 1,
                        of=attempts,
                        sleep_s=sleep_s,
                        error=str(e),
                        error_type=type(e).__name__,
                    )
                    await asyncio.sleep(sleep_s)
                else:
                    logger.error(
                        "place_order_err",
                        m=order.market_id[:12],
                        error=str(e),
                        error_type=type(e).__name__,
                    )

        self._emit_exec_block(
            order, BlockReason.NO_FEED,
            f"place_order failed after {attempts} attempts: {last_err}",
        )
        order.status = OrderStatus.REJECTED
        return order

    # ─────────────────────────────────────────────────────────────────
    #  Dual-direction arb (atomic two-leg)
    # ─────────────────────────────────────────────────────────────────

    async def _execute_dual_leg(
        self,
        yes_order: Order,
        portfolio: Portfolio,
        meta: dict,
    ) -> Order:
        """Place YES leg, then NO leg. Roll back YES if NO fails."""
        no_token_id = meta.get("no_token_id", "")
        no_implied_ask = float(meta.get("no_implied_ask", 0.0))
        legs_max_age_ms = int(meta.get("legs_max_age_ms", 500))

        if not no_token_id or no_implied_ask <= 0:
            logger.warning(
                "dual_leg_missing_metadata",
                m=yes_order.market_id[:12],
                no_tok=bool(no_token_id),
                no_ask=no_implied_ask,
            )
            self._emit_exec_block(yes_order, BlockReason.NO_SIGNAL,
                                  "dual_direction: missing no_token_id")
            yes_order.status = OrderStatus.REJECTED
            return yes_order

        no_order = Order(
            market_id=yes_order.market_id,
            token_id=no_token_id,
            side=Side.BUY,
            price=no_implied_ask,
            size=yes_order.size,
            order_type=yes_order.order_type,
            strategy=f"{yes_order.strategy}_no_leg",
            signal_id=yes_order.signal_id,
            metadata={"is_dual_direction_no_leg": True},
        )

        if self._is_paper:
            yes_filled = await self._simulate_paper_fill(yes_order)
            # _simulate_paper_fill emits order_filled internally for each leg;
            # do not double-emit the no leg here.
            await self._simulate_paper_fill(no_order)
            return yes_filled

        # Live path
        await self._rate_limit()
        t0 = time.time()
        yes_placed = await self._submit_live_with_retry(yes_order)
        if yes_placed.status == OrderStatus.REJECTED:
            return yes_placed

        elapsed_ms = (time.time() - t0) * 1000
        if elapsed_ms > legs_max_age_ms:
            logger.warning(
                "dual_yes_leg_too_slow",
                m=yes_order.market_id[:12],
                elapsed_ms=round(elapsed_ms, 1),
                deadline_ms=legs_max_age_ms,
            )
            await self._safe_cancel(yes_placed)
            yes_placed.status = OrderStatus.CANCELLED
            return yes_placed

        no_placed = await self._submit_live_with_retry(no_order)
        if no_placed.status == OrderStatus.REJECTED:
            logger.error(
                "dual_no_leg_failed_rolling_back_yes",
                m=yes_order.market_id[:12],
            )
            await self._safe_cancel(yes_placed)
            yes_placed.status = OrderStatus.CANCELLED
            return yes_placed

        await self._event_bus.emit("order_filled", order=no_placed)
        return yes_placed

    async def _safe_cancel(self, order: Order) -> None:
        try:
            await self._client.cancel_order(order.order_id, market_id=order.market_id)
        except Exception as e:
            logger.warning(
                "rollback_cancel_failed",
                order_id=getattr(order, "order_id", "?")[:16],
                error=str(e),
            )

    # ─────────────────────────────────────────────────────────────────
    #  Paper-mode simulation
    # ─────────────────────────────────────────────────────────────────

    async def _simulate_paper_fill(self, order: Order) -> Order:
        """Simulate a fill at the limit price."""
        order.order_id = order.order_id or f"paper-{uuid.uuid4().hex[:12]}"
        order.filled_size = order.size
        order.avg_fill_price = order.price
        order.status = OrderStatus.FILLED
        order.created_at = datetime.utcnow()

        try:
            await self._event_bus.emit("order_filled", order=order)
        except Exception as e:
            logger.warning("paper_fill_emit_err", error=str(e))

        logger.info(
            "paper_fill",
            m=order.market_id[:12],
            side=order.side.value,
            px=round(order.price, 4),
            sz=round(order.size, 2),
            strat=order.strategy,
        )
        return order

    # ─────────────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────────────

    async def _rate_limit(self) -> None:
        """Block until under the per-second rate-limit ceiling."""
        if self._config.rate_limit_per_second <= 0:
            return
        now = time.time()
        self._last_request_ts = [t for t in self._last_request_ts if now - t < 1.0]
        if len(self._last_request_ts) >= self._config.rate_limit_per_second:
            wait = 1.0 - (now - self._last_request_ts[0])
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_request_ts.append(time.time())

    def _emit_exec_block(self, order: Order, reason: str, note: str) -> None:
        try:
            cycle_id = f"{int(time.time() * 1000) % 100000:05d}"
            emit(
                cycle_id=cycle_id,
                strategy=f"exec({order.strategy})",
                market_id=order.market_id,
                mid=order.price,
                size_usd=order.size * order.price,
                decision="BLOCKED",
                reason=reason,
                extra={"note": note},
            )
        except Exception:
            pass
