"""Maker edge — passive liquidity provision.

PATCHED v2 — fixes from first run observation:
The previous version fired the SAME signal every 500ms because:
  1. _inventory was never updated (no fill hook)
  2. Same orderbook (WS frozen) → same mid/spread → same target → same signal
  3. Cooldown was per-(strategy, market) at 1s; maker fired every 1s for 13s
     and accumulated $195 of position before portfolio cap stopped it

Fixes:
  1. update_inventory() is now actually called (wire-up in main._on_order_filled
     adds an inventory tracker per (strategy, market))
  2. New _last_quote_ts per market — won't re-quote within 5s on same market
     unless mid/spread materially changed (>2 bps move)
  3. Self-imposed notional cap: tracks dollar value of open inventory and
     refuses to add more than max_position_notional_usd ($30 default)
  4. Decision-log line now includes inventory_notional_usd for visibility
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
        # NEW: dollar cap per market — prevents the $195 runaway
        max_position_notional_usd: float = 30.0,
        # NEW: minimum seconds between quotes on same market (was implicit cooldown)
        min_quote_interval_s: float = 5.0,
        # NEW: skip re-quote if mid moved less than this many cents
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

        # Per-market state
        self._inventory_shares: dict[str, float] = {}      # net shares (+long YES, -short YES)
        self._inventory_notional: dict[str, float] = {}    # dollar notional of open inventory
        self._last_quote_ts: dict[str, float] = {}         # last quote emit time
        self._last_quote_mid: dict[str, float] = {}        # last quote's mid

    @property
    def name(self) -> str:
        return "maker_edge"

    def update_inventory(self, market_id: str, delta_shares: float, fill_price: float) -> None:
        """Hook for the bot to inform us of fills.

        Called from main._on_order_filled when a maker_edge order fills.
        delta_shares: positive=we got long YES, negative=we got short YES
        """
        self._inventory_shares[market_id] = (
            self._inventory_shares.get(market_id, 0.0) + delta_shares
        )
        # Notional uses absolute value — we count both long and short as "exposure"
        self._inventory_notional[market_id] = (
            self._inventory_notional.get(market_id, 0.0)
            + abs(delta_shares) * fill_price
        )

    def reset_inventory(self, market_id: str) -> None:
        """Called when a position closes."""
        self._inventory_shares.pop(market_id, None)
        self._inventory_notional.pop(market_id, None)
        self._last_quote_ts.pop(market_id, None)
        self._last_quote_mid.pop(market_id, None)

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

        # ── NEW: Notional cap on this market's inventory ─────────────
        current_notional = self._inventory_notional.get(market_id, 0.0)
        if current_notional >= self._max_position_notional:
            common["reason"] = BlockReason.POSITION_CAP
            emit(**common, extra={
                "inventory_notional_usd": current_notional,
                "cap_usd": self._max_position_notional,
            })
            return None

        # ── NEW: Anti-spam quote interval ────────────────────────────
        now_ts = time.time()
        last_ts = self._last_quote_ts.get(market_id, 0.0)
        last_mid = self._last_quote_mid.get(market_id, 0.0)
        if last_ts > 0:
            elapsed = now_ts - last_ts
            mid_change = abs(mid - last_mid)
            # If we recently quoted AND mid hasn't moved meaningfully → skip
            if elapsed < self._min_quote_interval and mid_change < self._min_mid_change:
                common["reason"] = BlockReason.COOLDOWN
                emit(**common, extra={
                    "elapsed_s": round(elapsed, 1),
                    "mid_change": round(mid_change, 4),
                    "min_interval": self._min_quote_interval,
                })
                return None

        # ── Inventory-aware side selection ───────────────────────────
        inventory_shares = self._inventory_shares.get(market_id, 0.0)
        imbalance = ob.book_imbalance

        # Long → prefer SELL; short → prefer BUY; flat → follow imbalance
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

        # Inventory cap check (shares-based)
        if direction == Direction.BUY and inventory_shares >= self._max_inventory:
            common["reason"] = BlockReason.POSITION_CAP
            emit(**common)
            return None
        if direction == Direction.SELL and inventory_shares <= -self._max_inventory:
            common["reason"] = BlockReason.POSITION_CAP
            emit(**common)
            return None

        # ── Quote price ──────────────────────────────────────────────
        skew_adj = self._inventory_skew * (
            inventory_shares / max(self._max_inventory, 1e-9)
        )
        half_spread = max(self._quote_offset, spread / 2.0 - 0.001)
        if direction == Direction.BUY:
            target_price = max(0.01, mid - half_spread - 0.005 * skew_adj)
        else:
            target_price = min(0.99, mid + half_spread + 0.005 * skew_adj)

        tick = 0.001 if (mid < 0.04 or mid > 0.96) else 0.01
        target_price = round(target_price / tick) * tick

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

        # Stamp this quote
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
