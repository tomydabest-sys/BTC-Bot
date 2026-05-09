"""Maker edge — passive liquidity provision.

PATCHED v3 (atop v2 patches):

The v2 fix introduced a notional cap to prevent runaway accumulation, but
update_inventory was double-counting on round trips:
  BUY 10 @ 0.5  → inv_shares=+10, inv_notional=$5
  SELL 10 @ 0.5 → inv_shares=  0, inv_notional=$10  (BUG: should be 0)

Fix: notional is now derived from |inv_shares| × last_price every time
update_inventory is called, replacing the stale accumulator. This is correct
for the intent ("how much exposure am I currently holding?") and self-corrects
when fills arrive in any order.
"""

from __future__ import annotations

import time
from datetime import datetime

import structlog

from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.diagnostics.decision_log import BlockReason, FORCE_TRADE, emit, relax
from polybot.strategies.base import BaseStrategy

logger = structlog.get_logger()


class MakerEdgeStrategy(BaseStrategy):
    """Posts inside-spread quotes to capture spread + maker rebate."""

    def __init__(
        self,
        min_spread: float = 0.008,
        quote_offset: float = 0.003,
        max_inventory: float = 0.30,
        inventory_skew: float = 0.5,
        size_pct: float = 0.04,
        time_of_day_filter: bool = False,
        volume_boost_threshold: float = 100.0,
        confidence_floor: float = 0.40,
        max_position_notional_usd: float = 30.0,
        min_quote_interval_s: float = 5.0,
        min_mid_change_to_requote: float = 0.005,
    ) -> None:
        self._min_spread = float(min_spread)
        self._quote_offset = float(quote_offset)
        self._max_inventory = float(max_inventory)
        self._inventory_skew = float(inventory_skew)
        self._size_pct = float(size_pct)
        self._time_of_day_filter = bool(time_of_day_filter)
        self._volume_boost_threshold = float(volume_boost_threshold)
        self._confidence_floor = float(confidence_floor)
        self._max_position_notional = float(max_position_notional_usd)
        self._min_quote_interval = float(min_quote_interval_s)
        self._min_mid_change = float(min_mid_change_to_requote)

        # Per-market inventory state.
        # _inventory_shares: signed share count (+long YES, -short YES)
        # _inventory_last_price: last fill price per market — used to recompute
        #     notional consistently from |shares| × price
        self._inventory_shares: dict[str, float] = {}
        self._inventory_last_price: dict[str, float] = {}
        self._last_quote_ts: dict[str, float] = {}
        self._last_quote_mid: dict[str, float] = {}

    @property
    def name(self) -> str:
        return "maker_edge"

    def update_inventory(
        self, market_id: str, delta_shares: float, fill_price: float,
    ) -> None:
        """Update inventory on a fill. Called by main._on_order_filled.

        delta_shares: positive=we got long YES, negative=we got short YES
        fill_price: price at which the fill occurred — used for notional calc
        """
        new_shares = self._inventory_shares.get(market_id, 0.0) + delta_shares
        # Snap to zero if rounding leftovers
        if abs(new_shares) < 1e-9:
            new_shares = 0.0
        self._inventory_shares[market_id] = new_shares
        if fill_price > 0:
            self._inventory_last_price[market_id] = fill_price

    def reset_inventory(self, market_id: str) -> None:
        """Called when a position closes (full exit)."""
        self._inventory_shares.pop(market_id, None)
        self._inventory_last_price.pop(market_id, None)
        self._last_quote_ts.pop(market_id, None)
        self._last_quote_mid.pop(market_id, None)

    def _current_notional_usd(self, market_id: str, mid_price: float) -> float:
        """Compute current absolute notional from signed shares × mid (fallback to last fill)."""
        shares = self._inventory_shares.get(market_id, 0.0)
        if shares == 0:
            return 0.0
        ref_price = mid_price if mid_price > 0 else self._inventory_last_price.get(
            market_id, 0.0
        )
        if ref_price <= 0:
            return 0.0
        return abs(shares) * ref_price

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

        try:
            now_dt = datetime.utcnow()
            end = snapshot.market.end_date
            if getattr(end, "tzinfo", None) is not None:
                end = end.replace(tzinfo=None)
            t_rem = (end - now_dt).total_seconds()
            common["time_to_expiry_s"] = t_rem
        except Exception:
            t_rem = 999.0

        # Don't quote in last 30s — adverse selection peaks
        if t_rem < 30.0:
            common["reason"] = BlockReason.TIME_REMAINING_TOO_LOW
            emit(**common)
            return None

        if ob.best_bid <= 0 or ob.best_ask >= 1.0:
            common["reason"] = BlockReason.INVALID_BOOK
            emit(**common)
            return None

        eff_min_spread = relax(self._min_spread, 0.5, floor=0.003)
        spread = ob.spread
        if spread < eff_min_spread:
            common["reason"] = BlockReason.SPREAD_TOO_NARROW
            emit(**common)
            return None

        mid = ob.mid_price
        if mid < 0.05 or mid > 0.95:
            common["reason"] = BlockReason.OUTSIDE_PRICE_BAND
            emit(**common)
            return None

        if snapshot.market.volume_24h < self._volume_boost_threshold * 0.1:
            common["reason"] = BlockReason.SPREAD_TOO_NARROW
            emit(**common)
            return None

        # Notional cap on this market's inventory (PATCHED — derived not stored)
        current_notional = self._current_notional_usd(market_id, mid)
        if current_notional >= self._max_position_notional:
            common["reason"] = BlockReason.POSITION_CAP
            emit(**common, extra={
                "inventory_notional_usd": current_notional,
                "cap_usd": self._max_position_notional,
            })
            return None

        # Anti-spam quote interval
        now_ts = time.time()
        last_ts = self._last_quote_ts.get(market_id, 0.0)
        last_mid = self._last_quote_mid.get(market_id, 0.0)
        if last_ts > 0:
            elapsed = now_ts - last_ts
            mid_change = abs(mid - last_mid)
            if elapsed < self._min_quote_interval and mid_change < self._min_mid_change:
                common["reason"] = BlockReason.COOLDOWN
                emit(**common, extra={
                    "elapsed_s": round(elapsed, 1),
                    "mid_change": round(mid_change, 4),
                    "min_interval": self._min_quote_interval,
                })
                return None

        # Inventory-aware side selection
        inventory_shares = self._inventory_shares.get(market_id, 0.0)
        imbalance = ob.book_imbalance

        if inventory_shares > self._max_inventory * 0.5:
            direction = Direction.SELL
            outcome = "Yes"
        elif inventory_shares < -self._max_inventory * 0.5:
            direction = Direction.BUY
            outcome = "Yes"
        elif imbalance > 0.15:
            direction = Direction.BUY
            outcome = "Yes"
        elif imbalance < -0.15:
            direction = Direction.SELL
            outcome = "Yes"
        else:
            common["reason"] = BlockReason.NO_SIGNAL
            emit(**common)
            return None

        if direction == Direction.BUY and inventory_shares >= self._max_inventory:
            common["reason"] = BlockReason.POSITION_CAP
            emit(**common)
            return None
        if direction == Direction.SELL and inventory_shares <= -self._max_inventory:
            common["reason"] = BlockReason.POSITION_CAP
            emit(**common)
            return None

        # Normalize inventory to [-1, 1] before applying skew. Without the
        # clamp, an out-of-cap inventory (e.g. -97 shares with max=0.20) blew
        # skew_adj to ~244 and pushed target_price to 1.29 — outside Polymarket's
        # [0, 1] range, fillable in paper mode, instant 95% loss on MTM.
        inv_ratio = inventory_shares / max(self._max_inventory, 1e-9)
        inv_ratio_clamped = max(-1.0, min(1.0, inv_ratio))
        skew_adj = self._inventory_skew * inv_ratio_clamped
        half_spread = max(self._quote_offset, spread / 2.0 - 0.001)
        if direction == Direction.BUY:
            target_price = mid - half_spread - 0.005 * skew_adj
        else:
            target_price = mid + half_spread + 0.005 * skew_adj
        # Hard-clamp to a tradable Polymarket price range. Defence in depth
        # against any future skew/half_spread regression.
        target_price = max(0.01, min(0.99, target_price))

        tick = 0.001 if (mid < 0.04 or mid > 0.96) else 0.01
        target_price = round(target_price / tick) * tick
        target_price = max(0.01, min(0.99, target_price))

        edge = half_spread
        edge_bps = edge * 10000
        common["edge_bps"] = edge_bps

        confidence = max(
            self._confidence_floor,
            min(0.85, 0.4 + edge * 4 + abs(imbalance) * 0.2),
        )
        common["confidence"] = confidence
        common["fair_value"] = mid
        common["decision"] = direction.value
        common["reason"] = BlockReason.OK

        self._last_quote_ts[market_id] = now_ts
        self._last_quote_mid[market_id] = mid

        emit(**common, extra={
            "inventory_shares": inventory_shares,
            "inventory_notional_usd": current_notional,
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
            inv=round(inventory_shares, 3),
            inv_usd=round(current_notional, 2),
            cap_usd=self._max_position_notional,
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
                f"mid={mid:.4f} spread={spread:.4f} "
                f"inv_shares={inventory_shares:+.3f} inv_usd=${current_notional:.2f}"
            ),
            metadata={
                "is_maker_only": True,
                "is_market_maker": True,
                "fair_value": mid,
                "edge_bps": edge_bps,
                "inventory_shares": inventory_shares,
                "inventory_notional_usd": current_notional,
                "imbalance": imbalance,
            },
        )

    def get_params(self) -> dict:
        return {
            "min_spread": self._min_spread,
            "quote_offset": self._quote_offset,
            "max_inventory_shares": self._max_inventory,
            "max_position_notional_usd": self._max_position_notional,
            "min_quote_interval_s": self._min_quote_interval,
            "inventory_skew": self._inventory_skew,
            "size_pct": self._size_pct,
            "confidence_floor": self._confidence_floor,
            "force_trade": FORCE_TRADE,
        }
