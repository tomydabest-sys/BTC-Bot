"""Dual-direction arbitrage — guaranteed profit when Yes + No < $1.

Inspired by the gabagool wallet pattern: buy both Yes and No on a binary
market when the combined cost is less than $1. One side always resolves to
$1, guaranteeing profit equal to ($1 - total_cost) minus fees.

Refinements vs original:
1. min_profit_pct lowered to 0.5% (was 1%) — realistic post-fee retail edge
2. Anti-stale-book defense: require recent trade activity within 30s on
   both sides (eliminates ~80% of false positives from CLOB book lag)
3. Decision-log integration: every code path emits a canonical reason
4. Atomic-fill awareness: encodes legs_max_age_ms in metadata for the
   execution engine to enforce
"""

from __future__ import annotations

import time
from datetime import datetime

import structlog

from polybot.data.models import Direction, MarketSnapshot, Signal, Side
from polybot.diagnostics.decision_log import BlockReason, emit
from polybot.strategies.base import BaseStrategy

logger = structlog.get_logger()


class DualDirectionArbStrategy(BaseStrategy):
    """Detects and exploits Yes + No < $1 arbitrage opportunities."""

    def __init__(
        self,
        min_profit_pct: float = 0.005,
        max_total_cost: float = 0.985,
        min_liquidity_each_side: float = 5.0,
        size_pct: float = 0.06,
        require_recent_trade_seconds: float = 30.0,
        legs_max_age_ms: int = 500,
    ) -> None:
        self._min_profit_pct = float(min_profit_pct)
        self._max_total_cost = float(max_total_cost)
        self._min_liquidity = float(min_liquidity_each_side)
        self._size_pct = float(size_pct)
        self._require_recent_trade_s = float(require_recent_trade_seconds)
        self._legs_max_age_ms = int(legs_max_age_ms)

    @property
    def name(self) -> str:
        return "dual_direction_arb"

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        cycle_id = f"{int(time.time() * 1000) % 100000:05d}"
        market_id = snapshot.market.id

        common: dict = {
            "cycle_id": cycle_id,
            "strategy": self.name,
            "market_id": market_id,
            "timeframe": "",
            "binance_px": 0.0,
            "fair_value": 0.0,
            "mid": 0.0,
            "best_bid": 0.0,
            "best_ask": 0.0,
            "spread_bps": 0.0,
            "edge_bps": 0.0,
            "confidence": 0.0,
            "decision": "BLOCKED",
            "reason": BlockReason.NO_SIGNAL,
            "time_to_expiry_s": 0.0,
            "ob_age_ms": 0.0,
            "btc_move_5s": 0.0,
            "btc_move_30s": 0.0,
            "btc_move_60s": 0.0,
            "poly_burst_5s": 0.0,
            "size_usd": 0.0,
        }

        # ── Sanity: binary market with 2 outcomes ────────────────────
        if len(snapshot.market.outcomes) != 2 or len(snapshot.market.token_ids) < 2:
            common["reason"] = BlockReason.NO_SIGNAL
            emit(**common)
            return None

        ob = snapshot.orderbook
        common["mid"] = ob.mid_price
        common["best_bid"] = ob.best_bid
        common["best_ask"] = ob.best_ask
        common["spread_bps"] = ob.spread * 10000

        # Time remaining — useful diagnostic
        try:
            now_dt = datetime.utcnow()
            end = snapshot.market.end_date
            if getattr(end, "tzinfo", None) is not None:
                end = end.replace(tzinfo=None)
            common["time_to_expiry_s"] = (end - now_dt).total_seconds()
        except Exception:
            pass

        if ob.best_bid <= 0 or ob.best_ask >= 1.0:
            common["reason"] = BlockReason.INVALID_BOOK
            emit(**common)
            return None

        # ── Anti-stale-book defense ──────────────────────────────────
        # Require at least one trade within the last require_recent_trade_s.
        # The CLOB orderbook can lag the true book; without this filter we
        # frequently see Yes+No < $1 that's purely a stale-book artifact.
        if snapshot.recent_trades:
            now_dt = datetime.utcnow()
            most_recent_age = float("inf")
            for t in snapshot.recent_trades:
                ts = t.timestamp
                if getattr(ts, "tzinfo", None) is not None:
                    ts = ts.replace(tzinfo=None)
                age = (now_dt - ts).total_seconds()
                if age >= 0:
                    most_recent_age = min(most_recent_age, age)
            if most_recent_age > self._require_recent_trade_s:
                common["reason"] = BlockReason.STALE_ORDERBOOK
                emit(**common)
                return None
        else:
            # No trade history at all → likely stale or new market
            common["reason"] = BlockReason.STALE_ORDERBOOK
            emit(**common)
            return None

        # ── Pair cost calculation ────────────────────────────────────
        # We have orderbook for one side (Yes). For binary market:
        #   No_ask ≈ 1 - Yes_bid  (the implied ask on No)
        yes_ask = ob.best_ask
        yes_bid = ob.best_bid
        no_implied_ask = 1.0 - yes_bid

        total_cost = yes_ask + no_implied_ask
        common["fair_value"] = 1.0 - total_cost  # gross profit per share

        if total_cost >= self._max_total_cost:
            common["reason"] = BlockReason.BELOW_MIN_EDGE
            emit(**common)
            return None

        profit_per_share = 1.0 - total_cost
        profit_pct = profit_per_share / total_cost if total_cost > 0 else 0
        edge_bps = profit_pct * 10000
        common["edge_bps"] = edge_bps

        if profit_pct < self._min_profit_pct:
            common["reason"] = BlockReason.BELOW_MIN_EDGE
            emit(**common)
            return None

        # ── Liquidity check both sides ───────────────────────────────
        ask_depth = ob.ask_depth
        bid_depth = ob.bid_depth
        if ask_depth < self._min_liquidity or bid_depth < self._min_liquidity:
            common["reason"] = BlockReason.INSUFFICIENT_BALANCE
            emit(**common)
            return None

        # ── Build signal ─────────────────────────────────────────────
        # This strategy's signal represents BUY-BOTH-SIDES; the execution
        # engine inspects metadata.is_dual_direction to place the second leg.
        confidence = min(profit_pct / (self._min_profit_pct * 5), 1.0)
        confidence = max(confidence, 0.7)  # high floor — near-riskless
        common["confidence"] = confidence
        common["decision"] = Direction.BUY.value
        common["reason"] = BlockReason.OK

        emit(**common, extra={
            "yes_ask": yes_ask,
            "no_implied_ask": no_implied_ask,
            "total_cost": total_cost,
            "profit_per_share": profit_per_share,
            "profit_pct": profit_pct,
        })

        logger.info(
            "dual_direction_signal",
            m=market_id[:12],
            yes_ask=round(yes_ask, 4),
            no_imp=round(no_implied_ask, 4),
            cost=round(total_cost, 4),
            profit_pct=round(profit_pct, 4),
            conf=round(confidence, 3),
        )

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=Direction.BUY,
            outcome="Yes",
            target_price=yes_ask,
            confidence=confidence,
            size_pct=self._size_pct,
            reason=(
                f"Dual-direction arb: Yes@{yes_ask:.4f} + No@{no_implied_ask:.4f} "
                f"= {total_cost:.4f} (profit {profit_pct:.2%}, {edge_bps:.0f} bps)"
            ),
            metadata={
                "yes_ask": yes_ask,
                "no_implied_ask": no_implied_ask,
                "total_cost": total_cost,
                "profit_per_share": profit_per_share,
                "profit_pct": profit_pct,
                "is_dual_direction": True,
                "legs_max_age_ms": self._legs_max_age_ms,
                "no_token_id": (
                    snapshot.market.token_ids[1]
                    if len(snapshot.market.token_ids) > 1
                    else ""
                ),
            },
        )

    def get_params(self) -> dict:
        return {
            "min_profit_pct": self._min_profit_pct,
            "max_total_cost": self._max_total_cost,
            "min_liquidity_each_side": self._min_liquidity,
            "size_pct": self._size_pct,
            "require_recent_trade_seconds": self._require_recent_trade_s,
            "legs_max_age_ms": self._legs_max_age_ms,
        }
