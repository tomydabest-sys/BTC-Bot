"""Overshoot reversion — fades post-burst Polymarket overextension.

PATCHED FROM ORIGINAL:
1. All thresholds wrapped in `relax()` helper for force-trade mode
2. Block reasons use stage-numbered codes (OVR_01..OVR_08) for sortable
   analyze.py output: at a glance you see which lifecycle stage is killing trades
3. WRONG_DIRECTION and BURST_NOT_BTC_DRIVEN gates SKIPPED in force-trade mode
4. confidence_floor effectively disabled in force-trade mode (set to 0.30)
"""

from __future__ import annotations

import math
import time
from datetime import datetime

import structlog

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.diagnostics.decision_log import (
    BlockReason,
    FORCE_TRADE,
    emit,
    relax,
)
from polybot.strategies.base import BaseStrategy
from polybot.strategies.fair_value import (
    fair_value as bs_fair_value,
    fee_aware_edge,
    realized_sigma_per_sec,
)

logger = structlog.get_logger()


_DIAG_INTERVAL = 10.0
_SUMMARY_INTERVAL = 60.0


class OvershootReversionStrategy(BaseStrategy):

    def __init__(
        self,
        min_btc_move_60s_pct: float = 0.0008,
        min_btc_move_30s_pct: float = 0.0005,
        min_poly_burst: float = 0.003,
        min_overshoot: float = 0.005,
        max_overshoot: float = 0.150,
        min_time_remaining: float = 60.0,
        max_time_remaining: float = 285.0,
        min_poly_mid: float = 0.15,
        max_poly_mid: float = 0.85,
        max_spread: float = 0.060,
        min_btc_move_confirm: float = 0.0003,
        size_pct: float = 0.05,
        confidence_floor: float = 0.40,
        window_seconds: float = 300.0,
        fair_value_model: str = "black_scholes",
        sigma_lookback_s: int = 900,
        sigma_halflife_s: int = 60,
        fee_theta_taker: float = 0.072,
        edge_floor_bps: float = 3.0,
    ) -> None:
        self._min_btc_move_60s = float(min_btc_move_60s_pct)
        self._min_btc_move_30s = float(min_btc_move_30s_pct)
        self._min_poly_burst = float(min_poly_burst)
        self._min_overshoot = float(min_overshoot)
        self._max_overshoot = float(max_overshoot)
        self._min_t_rem = float(min_time_remaining)
        self._max_t_rem = float(max_time_remaining)
        self._min_mid = float(min_poly_mid)
        self._max_mid = float(max_poly_mid)
        self._max_spread = float(max_spread)
        self._min_btc_move = float(min_btc_move_confirm)
        self._size_pct = float(size_pct)
        self._confidence_floor = float(confidence_floor)
        self._window_seconds = float(window_seconds)
        self._fair_value_model = str(fair_value_model)
        self._sigma_lookback = int(sigma_lookback_s)
        self._sigma_halflife = int(sigma_halflife_s)
        self._fee_theta = float(fee_theta_taker)
        self._edge_floor_bps = float(edge_floor_bps)
        self._feed: PriceFeedState | None = None
        self._last_diag: dict[str, float] = {}
        self._last_summary: float = 0.0
        self._cycles: int = 0
        self._blocks: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "overshoot_reversion"

    def set_exchange_feed(self, feed: PriceFeedState) -> None:
        self._feed = feed

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        self._cycles += 1
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

        # ── Stage 01: Feed warming ───────────────────────────────────
        # In force-trade mode, drop tick requirement from 60 to 30
        min_ticks = relax(60.0, 0.5, floor=20.0)
        if self._feed is None or len(self._feed.ticks) < min_ticks:
            common["reason"] = BlockReason.OVR_01_FEED_WARMING
            self._count_block(BlockReason.OVR_01_FEED_WARMING)
            emit(**common)
            return None

        common["binance_px"] = float(self._feed.last_price)

        # ── Stage 02: Time remaining ─────────────────────────────────
        now_dt = datetime.utcnow()
        end = snapshot.market.end_date
        if getattr(end, "tzinfo", None) is not None:
            end = end.replace(tzinfo=None)
        t_rem = (end - now_dt).total_seconds()
        common["time_to_expiry_s"] = t_rem

        # In force-trade mode, widen time gates
        eff_min_t = relax(self._min_t_rem, 0.5, floor=20.0)
        eff_max_t = self._max_t_rem if not FORCE_TRADE else self._max_t_rem + 30
        if t_rem < eff_min_t or t_rem > eff_max_t:
            common["reason"] = BlockReason.OVR_02_TIME_RANGE
            self._count_block(BlockReason.OVR_02_TIME_RANGE)
            emit(**common)
            return None

        # ── Stage 03: Orderbook sanity ───────────────────────────────
        ob = snapshot.orderbook
        mid = ob.mid_price
        common["mid"] = mid
        common["best_bid"] = ob.best_bid
        common["best_ask"] = ob.best_ask
        common["spread_bps"] = ob.spread * 10000

        if ob.best_bid <= 0 or ob.best_ask >= 1.0:
            common["reason"] = BlockReason.OVR_03_BOOK_INVALID
            self._count_block(BlockReason.OVR_03_BOOK_INVALID)
            emit(**common)
            return None

        eff_min_mid = relax(self._min_mid, 0.7, floor=0.05)
        eff_max_mid = 1.0 - eff_min_mid
        if not (eff_min_mid < mid < eff_max_mid):
            common["reason"] = BlockReason.OUTSIDE_PRICE_BAND
            self._count_block(BlockReason.OUTSIDE_PRICE_BAND)
            emit(**common)
            return None

        eff_max_spread = self._max_spread * (2.0 if FORCE_TRADE else 1.0)
        if ob.spread > eff_max_spread:
            common["reason"] = BlockReason.SPREAD_TOO_WIDE
            self._count_block(BlockReason.SPREAD_TOO_WIDE)
            emit(**common)
            return None

        # ── Stage 04: BTC move features ──────────────────────────────
        btc_move_5s = float(self._feed.price_change_since(5.0))
        btc_move_10s = float(self._feed.price_change_since(10.0))
        btc_move_30s = float(self._feed.price_change_since(30.0))
        btc_move_60s = float(self._feed.price_change_since(60.0))
        common["btc_move_5s"] = btc_move_5s
        common["btc_move_30s"] = btc_move_30s
        common["btc_move_60s"] = btc_move_60s

        # ── Stage 05: Polymarket burst ──────────────────────────────
        poly_burst = float(getattr(snapshot, "poly_move_5s", 0.0) or 0.0)
        common["poly_burst_5s"] = poly_burst

        eff_min_burst = relax(self._min_poly_burst, 0.5, floor=0.001)
        if abs(poly_burst) < eff_min_burst:
            common["reason"] = BlockReason.OVR_05_NO_BURST
            self._count_block(BlockReason.OVR_05_NO_BURST)
            emit(**common)
            return None

        # ── BTC move confirmation ────────────────────────────────────
        eff_min_btc = relax(self._min_btc_move, 0.5, floor=0.0001)
        if abs(btc_move_10s) < eff_min_btc:
            common["reason"] = BlockReason.OVR_04_BTC_MOVE
            self._count_block(BlockReason.OVR_04_BTC_MOVE)
            emit(**common)
            return None

        # ── Stage 06: Direction consistency (SKIPPED in force-trade mode) ──
        if not FORCE_TRADE:
            if (poly_burst > 0) != (btc_move_10s > 0):
                common["reason"] = BlockReason.OVR_06_DIRECTION
                self._count_block(BlockReason.OVR_06_DIRECTION)
                emit(**common)
                return None

        # ── Realized vol → fair value ────────────────────────────────
        sigma_per_sec = self._compute_sigma_per_sec()
        if sigma_per_sec <= 0:
            common["reason"] = BlockReason.BTC_VOL_ZERO
            self._count_block(BlockReason.BTC_VOL_ZERO)
            emit(**common)
            return None

        elapsed_in_window = max(5.0, min(self._window_seconds, self._window_seconds - t_rem))
        btc_move_window = float(self._feed.price_change_since(elapsed_in_window))

        spot = float(self._feed.last_price)
        if spot <= 0:
            common["reason"] = BlockReason.BTC_FEED_INVALID
            self._count_block(BlockReason.BTC_FEED_INVALID)
            emit(**common)
            return None

        strike = spot / (1.0 + btc_move_window) if (1.0 + btc_move_window) > 0 else spot
        fair = bs_fair_value(spot, strike, t_rem, sigma_per_sec)
        common["fair_value"] = fair

        overshoot = mid - fair

        # In force-trade mode, skip the burst-direction = overshoot-direction check
        if not FORCE_TRADE:
            if (overshoot > 0) != (poly_burst > 0):
                common["reason"] = BlockReason.OVR_06_DIRECTION
                self._count_block(BlockReason.OVR_06_DIRECTION)
                emit(**common)
                return None

        # ── Stage 07: Overshoot magnitude ────────────────────────────
        abs_os = abs(overshoot)
        eff_min_os = relax(self._min_overshoot, 0.5, floor=0.002)
        if abs_os < eff_min_os:
            common["reason"] = BlockReason.OVR_07_OVERSHOOT_RANGE
            self._count_block(BlockReason.OVR_07_OVERSHOOT_RANGE)
            emit(**common)
            self._maybe_diag(
                market_id, mid, fair, poly_burst, btc_move_window,
                overshoot, t_rem, BlockReason.OVR_07_OVERSHOOT_RANGE,
            )
            return None
        if abs_os > self._max_overshoot:
            common["reason"] = BlockReason.OVERSHOOT_TOO_LARGE
            self._count_block(BlockReason.OVERSHOOT_TOO_LARGE)
            emit(**common)
            return None

        # ── Stage 08: Fee-aware edge ────────────────────────────────
        if overshoot > 0:
            net_edge = fee_aware_edge(
                model_p=1.0 - fair,
                mid=1.0 - mid,
                exit_p_estimate=0.5,
                theta=self._fee_theta,
            )
        else:
            net_edge = fee_aware_edge(
                model_p=fair,
                mid=mid,
                exit_p_estimate=0.5,
                theta=self._fee_theta,
            )

        edge_bps = net_edge * 10000
        common["edge_bps"] = edge_bps

        eff_edge_floor = relax(self._edge_floor_bps, 0.3, floor=1.0)
        if edge_bps < eff_edge_floor:
            common["reason"] = BlockReason.OVR_08_EDGE
            self._count_block(BlockReason.OVR_08_EDGE)
            emit(**common)
            return None

        # ── Direction + entry price ──────────────────────────────────
        if overshoot > 0:
            direction = Direction.SELL
            outcome = "No"
            target_price = ob.best_bid
            if target_price < eff_min_mid:
                common["reason"] = BlockReason.OUTSIDE_PRICE_BAND
                self._count_block(BlockReason.OUTSIDE_PRICE_BAND)
                emit(**common)
                return None
        else:
            direction = Direction.BUY
            outcome = "Yes"
            target_price = ob.best_ask
            if target_price > eff_max_mid:
                common["reason"] = BlockReason.OUTSIDE_PRICE_BAND
                self._count_block(BlockReason.OUTSIDE_PRICE_BAND)
                emit(**common)
                return None

        # ── Confidence ───────────────────────────────────────────────
        burst_score = min(abs(poly_burst) / 0.06, 1.0)
        overshoot_score = min(abs_os / 0.08, 1.0)
        time_score = min(t_rem / 180.0, 1.0)
        edge_score = min(edge_bps / 100.0, 1.0)
        confidence = (
            0.25
            + 0.30 * burst_score
            + 0.20 * overshoot_score
            + 0.10 * time_score
            + 0.15 * edge_score
        )

        # In force-trade mode, the confidence floor drops to 0.30
        eff_floor = relax(self._confidence_floor, 0.6, floor=0.30)
        confidence = max(eff_floor, min(confidence, 0.92))
        common["confidence"] = confidence

        size_pct_final = self._size_pct * confidence
        common["decision"] = direction.value
        common["reason"] = BlockReason.OK

        reason_str = (
            f"burst={poly_burst:+.4f} btc10s={btc_move_10s:+.4%} "
            f"fair={fair:.4f} mid={mid:.4f} os={overshoot:+.4f} "
            f"edge={edge_bps:.1f}bps t_rem={t_rem:.0f}s sigma={sigma_per_sec:.2e}/s"
        )

        emit(**common, extra={
            "btc_move_window": btc_move_window,
            "sigma_per_sec": sigma_per_sec,
            "overshoot": overshoot,
        })

        logger.info(
            "overshoot_signal",
            m=market_id[:12],
            dir=direction.value,
            mid=round(mid, 4),
            fair=round(fair, 4),
            os=round(overshoot, 4),
            burst=round(poly_burst, 4),
            edge_bps=round(edge_bps, 1),
            t_rem=round(t_rem, 0),
            conf=round(confidence, 3),
        )

        self._emit_summary()

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=target_price,
            confidence=confidence,
            size_pct=size_pct_final,
            reason=reason_str,
            metadata={
                "poly_burst": poly_burst,
                "btc_move_10s": btc_move_10s,
                "btc_move_window": btc_move_window,
                "sigma_per_sec": sigma_per_sec,
                "fair_value": fair,
                "overshoot": overshoot,
                "edge_bps": edge_bps,
                "t_rem": t_rem,
                "entry_mid": mid,
                "is_fade": True,
                "fair_value_model": self._fair_value_model,
            },
        )

    # ─────────────────────────────────────────────────────────────────
    #  Internals
    # ─────────────────────────────────────────────────────────────────

    def _compute_sigma_per_sec(self) -> float:
        if self._feed is None or not self._feed.ticks:
            return 0.0

        now_ts = time.time()
        cutoff = now_ts - self._sigma_lookback
        per_sec: dict[int, float] = {}
        for tick in self._feed.ticks:
            if tick.timestamp < cutoff:
                continue
            sec = int(tick.timestamp)
            per_sec[sec] = tick.price

        if len(per_sec) < 30:
            return realized_sigma_per_sec([], lookback_s=self._sigma_lookback)

        sorted_secs = sorted(per_sec)
        prices = [per_sec[s] for s in sorted_secs]
        return realized_sigma_per_sec(
            prices,
            lookback_s=self._sigma_lookback,
            halflife_s=self._sigma_halflife,
        )

    def _count_block(self, reason: str) -> None:
        self._blocks[reason] = self._blocks.get(reason, 0) + 1

    def _maybe_diag(
        self,
        market_id: str,
        mid: float,
        fair: float,
        burst: float,
        btc_move_window: float,
        overshoot: float,
        t_rem: float,
        block_reason: str,
    ) -> None:
        now = time.time()
        last = self._last_diag.get(market_id, 0.0)
        if now - last < _DIAG_INTERVAL:
            return
        if abs(overshoot) < self._min_overshoot * 0.4:
            return
        self._last_diag[market_id] = now
        logger.info(
            "overshoot_diag",
            m=market_id[:12],
            mid=round(mid, 4),
            fair=round(fair, 4),
            os=round(overshoot, 4),
            burst=round(burst, 4),
            btc_w=round(btc_move_window, 5),
            t_rem=round(t_rem, 0),
            block=block_reason,
        )

    def _emit_summary(self) -> None:
        now = time.time()
        if now - self._last_summary < _SUMMARY_INTERVAL:
            return
        self._last_summary = now
        if self._cycles > 0:
            logger.info(
                "overshoot_summary",
                cycles=self._cycles,
                blocks=self._blocks,
                force_trade=FORCE_TRADE,
            )
        self._cycles = 0
        self._blocks = {}

    def get_params(self) -> dict:
        return {
            "min_btc_move_60s_pct": self._min_btc_move_60s,
            "min_btc_move_30s_pct": self._min_btc_move_30s,
            "min_poly_burst": self._min_poly_burst,
            "min_overshoot": self._min_overshoot,
            "max_overshoot": self._max_overshoot,
            "min_time_remaining": self._min_t_rem,
            "max_time_remaining": self._max_t_rem,
            "entry_range": f"{self._min_mid}-{self._max_mid}",
            "max_spread": self._max_spread,
            "min_btc_move_confirm": self._min_btc_move,
            "size_pct": self._size_pct,
            "confidence_floor": self._confidence_floor,
            "fair_value_model": self._fair_value_model,
            "sigma_lookback_s": self._sigma_lookback,
            "sigma_halflife_s": self._sigma_halflife,
            "fee_theta_taker": self._fee_theta,
            "edge_floor_bps": self._edge_floor_bps,
            "force_trade": FORCE_TRADE,
        }
