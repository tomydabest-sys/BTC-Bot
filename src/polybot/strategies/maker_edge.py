"""Maker edge — passive liquidity provision.

Posts limit orders inside the bid-ask spread to capture spread + Polymarket
maker rebate. Maker fee on Polymarket is ~0% with daily USDC rebate of
20-25% of taker fees on liquid markets (per Polymarket docs).

Avellaneda-Stoikov-lite quoting:
    bid = mid - max(quote_offset, spread/2 - epsilon) - skew*inventory
    ask = mid + max(quote_offset, spread/2 - epsilon) + skew*inventory

We emit ONE signal per call, choosing the side that improves our inventory
position (reduces |inventory|) when we already hold a position; otherwise
we alternate by toggling on book imbalance.

Decision-log emit on every code path.
"""

from __future__ import annotations

import time
from datetime import datetime

import structlog

from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.diagnostics.decision_log import BlockReason, emit
from polybot.strategies.base import BaseStrategy

logger = structlog.get_logger()


class MakerEdgeStrategy(BaseStrategy):
    """Posts inside-spread quotes to capture spread + maker rebate."""

    def __init__(
        self,
        min_spread: float = 0.015,
        quote_offset: float = 0.005,
        max_inventory: float = 0.20,
        inventory_skew: float = 0.5,
        size_pct: float = 0.04,
        time_of_day_filter: bool = True,
        volume_boost_threshold: float = 5000.0,
        confidence_floor: float = 0.55,
    ) -> None:
        self._min_spread = float(min_spread)
        self._quote_offset = float(quote_offset)
        self._max_inventory = float(max_inventory)
        self._inventory_skew = float(inventory_skew)
        self._size_pct = float(size_pct)
        self._time_of_day_filter = bool(time_of_day_filter)
        self._volume_boost_threshold = float(volume_boost_threshold)
        self._confidence_floor = float(confidence_floor)
        # Track per-market net inventory (+long YES, -short YES). Updated
        # opportunistically by the bot when fills happen — for now it stays
        # zero so we alternate purely on book imbalance.
        self._inventory: dict[str, float] = {}

    @property
    def name(self) -> str:
        return "maker_edge"

    def update_inventory(self, market_id: str, delta_yes: float) -> None:
        """Hook for the bot to inform us of fills (positive=long YES)."""
        self._inventory[market_id] = self._inventory.get(market_id, 0.0) + delta_yes

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        cycle_id = f"{int(time.time() * 1000) % 100000:05d}"
        market_id = snapshot.market.id
        ob = snapshot.orderbook

        common: dict = {
            "cycle_id": cycle_id,
            "strategy": self.name,
            "market_id": market_id,
            "timeframe": "",
            "binance_px": 0.0,
            "fair_value": 0.0,
            "mid": ob.mid_price,
            "best_bid": ob.best_bid,
            "best_ask": ob.best_ask,
            "spread_bps": ob.spread * 10000,
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

        # Time remaining
        try:
            now_dt = datetime.utcnow()
            end = snapshot.market.end_date
            if getattr(end, "tzinfo", None) is not None:
                end = end.replace(tzinfo=None)
            t_rem = (end - now_dt).total_seconds()
            common["time_to_expiry_s"] = t_rem
        except Exception:
            t_rem = 999.0

        # Don't quote in last 30s — adverse selection peaks near expiry
        if t_rem < 30.0:
            common["reason"] = BlockReason.TIME_REMAINING_TOO_LOW
            emit(**common)
            return None

        if ob.best_bid <= 0 or ob.best_ask >= 1.0:
            common["reason"] = BlockReason.INVALID_BOOK
            emit(**common)
            return None

        spread = ob.spread
        if spread < self._min_spread:
            common["reason"] = BlockReason.SPREAD_TOO_NARROW
            emit(**common)
            return None

        mid = ob.mid_price
        # Skip extreme price bands — single-sided books, low liquidity
        if mid < 0.10 or mid > 0.90:
            common["reason"] = BlockReason.OUTSIDE_PRICE_BAND
            emit(**common)
            return None

        # Volume gate: only quote on markets with at least some volume so we
        # don't post into stale books.
        if snapshot.market.volume_24h < self._volume_boost_threshold * 0.1:
            common["reason"] = BlockReason.SPREAD_TOO_NARROW  # proxy for no flow
            emit(**common)
            return None

        # ── Inventory-aware side selection ───────────────────────────
        inventory = self._inventory.get(market_id, 0.0)
        # If long, prefer SELL (post ask); if short, prefer BUY (post bid).
        # If flat, follow book imbalance toward the heavier side.
        imbalance = ob.book_imbalance  # +1 bid-heavy, -1 ask-heavy

        if inventory > self._max_inventory * 0.5:
            direction = Direction.SELL
            outcome = "Yes"  # Selling YES = posting an ask
        elif inventory < -self._max_inventory * 0.5:
            direction = Direction.BUY
            outcome = "Yes"
        elif imbalance > 0.15:
            # Bid-heavy → mid likely to drift up → post bid (we want to BUY low)
            direction = Direction.BUY
            outcome = "Yes"
        elif imbalance < -0.15:
            direction = Direction.SELL
            outcome = "Yes"
        else:
            # Flat market, flat inventory → skip
            common["reason"] = BlockReason.NO_SIGNAL
            emit(**common)
            return None

        # ── Inventory cap check ──────────────────────────────────────
        if direction == Direction.BUY and inventory >= self._max_inventory:
            common["reason"] = BlockReason.POSITION_CAP
            emit(**common)
            return None
        if direction == Direction.SELL and inventory <= -self._max_inventory:
            common["reason"] = BlockReason.POSITION_CAP
            emit(**common)
            return None

        # ── Quote price (Avellaneda-Stoikov-lite) ────────────────────
        skew_adj = self._inventory_skew * (inventory / max(self._max_inventory, 1e-9))
        half_spread = max(self._quote_offset, spread / 2.0 - 0.001)
        if direction == Direction.BUY:
            target_price = max(0.01, mid - half_spread - 0.005 * skew_adj)
        else:
            target_price = min(0.99, mid + half_spread + 0.005 * skew_adj)

        # Snap to tick (0.01 generally; 0.001 in extremes)
        tick = 0.001 if (mid < 0.04 or mid > 0.96) else 0.01
        target_price = round(target_price / tick) * tick

        # ── Edge estimate: half-spread captured if filled at this price ──
        # Maker rebate ≈ 0% nominal; the edge is the spread we capture if the
        # opposite-side flow eventually crosses us.
        edge = half_spread  # cents per share
        edge_bps = edge * 10000
        common["edge_bps"] = edge_bps

        confidence = max(self._confidence_floor, min(0.85, 0.4 + edge * 4 + abs(imbalance) * 0.2))
        common["confidence"] = confidence
        common["fair_value"] = mid  # for maker, "fair" ≈ mid
        common["decision"] = direction.value
        common["reason"] = BlockReason.OK

        emit(**common, extra={
            "inventory": inventory,
            "imbalance": imbalance,
            "half_spread": half_spread,
            "target_price": target_price,
        })

        logger.info(
            "maker_signal",
            m=market_id[:12],
            dir=direction.value,
            target=round(target_price, 4),
            mid=round(mid, 4),
            spread=round(spread, 4),
            inv=round(inventory, 3),
            conf=round(confidence, 3),
        )

        return Signal(
            market_id=market_id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=target_price,
            confidence=confidence,
            size_pct=self._size_pct * confidence,
            reason=(
                f"Maker quote {direction.value}@{target_price:.4f} "
                f"mid={mid:.4f} spread={spread:.4f} inv={inventory:+.3f}"
            ),
            metadata={
                "is_maker_only": True,
                "is_market_maker": True,
                "fair_value": mid,
                "edge_bps": edge_bps,
                "inventory": inventory,
                "imbalance": imbalance,
            },
        )

    def get_params(self) -> dict:
        return {
            "min_spread": self._min_spread,
            "quote_offset": self._quote_offset,
            "max_inventory": self._max_inventory,
            "inventory_skew": self._inventory_skew,
            "size_pct": self._size_pct,
            "time_of_day_filter": self._time_of_day_filter,
            "volume_boost_threshold": self._volume_boost_threshold,
            "confidence_floor": self._confidence_floor,
        }
