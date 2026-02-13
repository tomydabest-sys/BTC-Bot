"""Pre-trade risk checks."""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from polybot.config import RiskConfig
from polybot.data.models import Order, Portfolio, RiskCheckResult, RiskDecision

logger = structlog.get_logger()


class RiskManager:
    """Validates every order against risk rules before execution."""

    def __init__(self, config: RiskConfig) -> None:
        self._config = config
        self._last_trade_times: dict[str, datetime] = {}

    def check_order(self, order: Order, portfolio: Portfolio) -> RiskCheckResult:
        """Run all pre-trade risk checks. Returns APPROVE, REJECT, or REDUCE."""
        checks = [
            self._check_order_size,
            self._check_position_size,
            self._check_portfolio_exposure,
            self._check_max_positions,
            self._check_daily_loss,
            self._check_trade_interval,
        ]

        for check in checks:
            result = check(order, portfolio)
            if result.decision != RiskDecision.APPROVE:
                logger.warning(
                    "risk_rejected",
                    check=check.__name__,
                    reason=result.reason,
                    order_market=order.market_id,
                    order_size=order.size,
                )
                return result

        logger.info("risk_approved", market=order.market_id, size=order.size)
        return RiskCheckResult(decision=RiskDecision.APPROVE)

    def record_trade(self, market_id: str) -> None:
        self._last_trade_times[market_id] = datetime.utcnow()

    def _check_order_size(self, order: Order, _portfolio: Portfolio) -> RiskCheckResult:
        notional = order.size * order.price
        if notional > self._config.max_order_size:
            return RiskCheckResult(
                decision=RiskDecision.REDUCE,
                reason=f"Order notional ${notional:.2f} exceeds max ${self._config.max_order_size}",
                modified_size=self._config.max_order_size / order.price,
            )
        return RiskCheckResult(decision=RiskDecision.APPROVE)

    def _check_position_size(self, order: Order, portfolio: Portfolio) -> RiskCheckResult:
        existing = sum(
            p.notional for p in portfolio.positions if p.market_id == order.market_id
        )
        new_notional = order.size * order.price
        if existing + new_notional > self._config.max_position_size:
            remaining = self._config.max_position_size - existing
            if remaining <= 0:
                return RiskCheckResult(
                    decision=RiskDecision.REJECT,
                    reason=f"Position limit reached for market {order.market_id}",
                )
            return RiskCheckResult(
                decision=RiskDecision.REDUCE,
                reason=f"Reducing to fit position limit",
                modified_size=remaining / order.price,
            )
        return RiskCheckResult(decision=RiskDecision.APPROVE)

    def _check_portfolio_exposure(self, order: Order, portfolio: Portfolio) -> RiskCheckResult:
        new_notional = order.size * order.price
        if portfolio.total_exposure + new_notional > self._config.max_portfolio_exposure:
            return RiskCheckResult(
                decision=RiskDecision.REJECT,
                reason=f"Portfolio exposure would exceed ${self._config.max_portfolio_exposure}",
            )
        return RiskCheckResult(decision=RiskDecision.APPROVE)

    def _check_max_positions(self, order: Order, portfolio: Portfolio) -> RiskCheckResult:
        market_ids = {p.market_id for p in portfolio.positions}
        if order.market_id not in market_ids and len(market_ids) >= self._config.max_positions:
            return RiskCheckResult(
                decision=RiskDecision.REJECT,
                reason=f"Max {self._config.max_positions} concurrent positions reached",
            )
        return RiskCheckResult(decision=RiskDecision.APPROVE)

    def _check_daily_loss(self, _order: Order, portfolio: Portfolio) -> RiskCheckResult:
        if portfolio.daily_pnl < -self._config.max_daily_loss:
            return RiskCheckResult(
                decision=RiskDecision.REJECT,
                reason=f"Daily loss ${abs(portfolio.daily_pnl):.2f} exceeds limit ${self._config.max_daily_loss}",
            )
        return RiskCheckResult(decision=RiskDecision.APPROVE)

    def _check_trade_interval(self, order: Order, _portfolio: Portfolio) -> RiskCheckResult:
        last = self._last_trade_times.get(order.market_id)
        if last:
            elapsed = (datetime.utcnow() - last).total_seconds()
            if elapsed < self._config.min_trade_interval_seconds:
                return RiskCheckResult(
                    decision=RiskDecision.REJECT,
                    reason=f"Too soon since last trade ({elapsed:.0f}s < {self._config.min_trade_interval_seconds}s)",
                )
        return RiskCheckResult(decision=RiskDecision.APPROVE)
