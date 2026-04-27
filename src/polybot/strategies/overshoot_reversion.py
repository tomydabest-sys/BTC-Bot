"""Overshoot reversion — fades post-burst Polymarket overextension.

PRIMARY strategy after the rescue plan rewrite. Replaces the saturating
sigmoid fair-value model with a Black-Scholes binary on EWMA realized vol,
retunes thresholds to spring-2026 BTC volatility regime, and emits a
canonical decision-log line on every code path.

Hypothesis: competing latency-arb bots create temporary impact on Polymarket
after sharp BTC moves. The aggregate impact pushes Polymarket past the rational
Black-Scholes fair value. Over 30–120s, flow normalizes and Polymarket retraces
30–60% of the overshoot. We fade the overshoot.

Signal:
  burst    = |poly_move_5s| >= min_poly_burst                 (8 mils default)
  fair     = N(d2) under EWMA realized vol                    (Black-Scholes binary)
  overshoot= mid - fair    (same sign as the burst)
  enter    = burst AND |overshoot| >= min_overshoot AND t_rem >= min_t_rem
            AND fee_aware_edge >= edge_floor_bps
  side     = -sign(overshoot)   (fade)

Exits (handled in PositionManager):
  TP       = entry-direction move >= 0.012   (reverted)
  Timeout  = position age >= 120s
  SL       = standard unrealized-loss stop
"""

from __future__ import annotations

import math
import time
from datetime import datetime

import structlog

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.diagnostics.decision_log import BlockReason, emit
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
    """Fades Polymarket bursts that exceed Black-Scholes-implied fair value."""

    def __init__(
        self,
        # BTC trigger
        min_btc_move_60s_pct: float = 0.0015,
        min_btc_move_30s_pct: float = 0.0010,
        # Polymarket burst
        min_poly_burst: float = 0.008,
        # Overshoot magnitude
        min_overshoot: float = 0.010,
        max_overshoot: float = 0.120,
        # Time gates
        min_time_remaining: float = 60.0,
        max_time_remaining: float = 270.0,
        # Entry band
        min_poly_mid: float = 0.15,
        max_poly_mid: float = 0.85,
        max_spread: float = 0.040,
        min_btc_move_confirm: float = 0.0005,
        # Sizing
        size_pct: float = 0.05,
        confidence_floor: float = 0.50,
        window_seconds: float = 300.0,
        # Fair value model
        fair_value_model: str = "black_scholes",
        sigma_lookback_s: int = 900,
        sigma_halflife_s: int = 60,
        # Fee awareness
        fee_theta_taker: float = 0.072,
        edge_floor_bps: float = 10.0,
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
        """Evaluate this market. Always emits one decision-log line."""
        self._cycles += 1
        cycle_id = f"{int(time.time() * 1000) % 100000:05d}"
        market_id = snapshot.market.id

        # Prepare common emit fields (filled in incrementally as we learn more)
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

        # ── Feed warming ─────────────────────────────────────────────
        if self._feed is None or len(self._feed.ticks) < 60:
            common["reason"] = BlockReason.FEED_WARMING
            self._count_block(BlockReason.FEED_WARMING)
            emit(**common)
            return None

        common["binance_px"] = float(self._feed.last_price)

        # ── Time remaining ───────────────────────────────────────────
        now_dt = datetime.utcnow()
        end = snapshot.market.end_date
        if getattr(end, "tzinfo", None) is not None:
            end = end.replace(tzinfo=None)
        t_rem = (end - now_dt).total_seconds()
        common["time_to_expiry_s"] = t_rem

        if t_rem < self._min_t_rem:
            common["reason"] = BlockReason.TIME_REMAINING_TOO_LOW
            self._count_block(BlockReason.TIME_REMAINING_TOO_LOW)
            emit(**common)
            return None
        if t_rem > self._max_t_rem:
            common["reason"] = BlockReason.TIME_REMAINING_TOO_HIGH
            self._count_block(BlockReason.TIME_REMAINING_TOO_HIGH)
            emit(**common)
            return None

        # ── Orderbook sanity ─────────────────────────────────────────
        ob = snapshot.orderbook
        mid = ob.mid_price
        common["mid"] = mid
        common["best_bid"] = ob.best_bid
        common["best_ask"] = ob.best_ask
        common["spread_bps"] = ob.spread * 10000

        if ob.best_bid <= 0 or ob.best_ask >= 1.0:
            common["reason"] = BlockReason.INVALID_BOOK
            self._count_block(BlockReason.INVALID_BOOK)
            emit(**common)
            return None

        if not (self._min_mid < mid < self._max_mid):
            common["reason"] = BlockReason.OUTSIDE_PRICE_BAND
            self._count_block(BlockReason.OUTSIDE_PRICE_BAND)
            emit(**common)
            return None

        if ob.spread > self._max_spread:
            common["reason"] = BlockReason.SPREAD_TOO_WIDE
            self._count_block(BlockReason.SPREAD_TOO_WIDE)
            emit(**common)
            return None

        # ── BTC move features ────────────────────────────────────────
        btc_move_5s = float(self._feed.price_change_since(5.0))
        btc_move_10s = float(self._feed.price_change_since(10.0))
        btc_move_30s = float(self._feed.price_change_since(30.0))
        btc_move_60s = float(self._feed.price_change_since(60.0))
        common["btc_move_5s"] = btc_move_5s
        common["btc_move_30s"] = btc_move_30s
        common["btc_move_60s"] = btc_move_60s

        # ── Polymarket burst ─────────────────────────────────────────
        poly_burst = float(getattr(snapshot, "poly_move_5s", 0.0) or 0.0)
        common["poly_burst_5s"] = poly_burst

        if abs(poly_burst) < self._min_poly_burst:
            common["reason"] = BlockReason.NO_BURST
            self._count_block(BlockReason.NO_BURST)
            emit(**common)
            return None

        # ── BTC move confirmation ────────────────────────────────────
        if abs(btc_move_10s) < self._min_btc_move:
            common["reason"] = BlockReason.BTC_MOVE_TOO_SMALL
            self._count_block(BlockReason.BTC_MOVE_TOO_SMALL)
            emit(**common)
            return None

        # Burst direction must match BTC direction
        if (poly_burst > 0) != (btc_move_10s > 0):
            common["reason"] = BlockReason.BURST_NOT_BTC_DRIVEN
            self._count_block(BlockReason.BURST_NOT_BTC_DRIVEN)
            emit(**common)
            return None

        # ── Realized vol → fair value ────────────────────────────────
        # Compute per-second EWMA realized vol from the BTC tick buffer.
        # We project tick prices onto a 1-Hz lattice via take-last-per-second.
        sigma_per_sec = self._compute_sigma_per_sec()
        if sigma_per_sec <= 0:
            common["reason"] = BlockReason.BTC_VOL_ZERO
            self._count_block(BlockReason.BTC_VOL_ZERO)
            emit(**common)
            return None

        # BTC move since window open (capped at window_seconds)
        elapsed_in_window = max(5.0, min(self._window_seconds, self._window_seconds - t_rem))
        btc_move_window = float(self._feed.price_change_since(elapsed_in_window))

        spot = float(self._feed.last_price)
        if spot <= 0:
            common["reason"] = BlockReason.BTC_FEED_INVALID
            self._count_block(BlockReason.BTC_FEED_INVALID)
            emit(**common)
            return None

        # Strike = spot at window open (best estimate from BTC move + current spot)
        strike = spot / (1.0 + btc_move_window) if (1.0 + btc_move_window) > 0 else spot

        fair = bs_fair_value(spot, strike, t_rem, sigma_per_sec)
        common["fair_value"] = fair

        overshoot = mid - fair  # positive = mid above fair (fade by selling Yes)

        # Burst direction must match overshoot sign (if BTC went up and Poly burst up,
        # but mid is still BELOW fair, that's underreaction not overreaction — abstain)
        if (overshoot > 0) != (poly_burst > 0):
            common["reason"] = BlockReason.WRONG_DIRECTION
            self._count_block(BlockReason.WRONG_DIRECTION)
            emit(**common)
            return None

        abs_os = abs(overshoot)
        if abs_os < self._min_overshoot:
            common["reason"] = BlockReason.OVERSHOOT_TOO_SMALL
            self._count_block(BlockReason.OVERSHOOT_TOO_SMALL)
            emit(**common)
            self._maybe_diag(
                market_id, mid, fair, poly_burst, btc_move_window,
                overshoot, t_rem, BlockReason.OVERSHOOT_TOO_SMALL,
            )
            return None
        if abs_os > self._max_overshoot:
            common["reason"] = BlockReason.OVERSHOOT_TOO_LARGE
            self._count_block(BlockReason.OVERSHOOT_TOO_LARGE)
            emit(**common)
            return None

        # ── Fee-aware edge ───────────────────────────────────────────
        if overshoot > 0:
            # Mid above fair → SELL No at best_bid (we want the implied "No" leg)
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

        if edge_bps < self._edge_floor_bps:
            common["reason"] = BlockReason.FEE_EXCEEDS_EDGE
            self._count_block(BlockReason.FEE_EXCEEDS_EDGE)
            emit(**common)
            return None

        # ── Direction + entry price ──────────────────────────────────
        if overshoot > 0:
            # Market too high → SELL (we want NO direction)
            direction = Direction.SELL
            outcome = "No"
            target_price = ob.best_bid
            if target_price < self._min_mid:
                common["reason"] = BlockReason.OUTSIDE_PRICE_BAND
                self._count_block(BlockReason.OUTSIDE_PRICE_BAND)
                emit(**common)
                return None
        else:
            # Market too low → BUY YES at best_ask
            direction = Direction.BUY
            outcome = "Yes"
            target_price = ob.best_ask
            if target_price > self._max_mid:
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
        confidence = max(self._confidence_floor, min(confidence, 0.92))
        common["confidence"] = confidence

        size_pct_final = self._size_pct * confidence
        common["size_usd"] = 0.0  # populated downstream by sizer
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
        """Compute EWMA per-second realized vol from the BTC tick buffer.

        We construct a 1-Hz price series by sampling the most recent tick of
        each second over the last sigma_lookback seconds, then call the shared
        realized_sigma_per_sec helper.
        """
        if self._feed is None or not self._feed.ticks:
            return 0.0

        now_ts = time.time()
        cutoff = now_ts - self._sigma_lookback
        # Ticks are timestamped; bucket by integer second (latest wins)
        per_sec: dict[int, float] = {}
        for tick in self._feed.ticks:
            if tick.timestamp < cutoff:
                continue
            sec = int(tick.timestamp)
            per_sec[sec] = tick.price

        if len(per_sec) < 30:
            return realized_sigma_per_sec([], lookback_s=self._sigma_lookback)

        # Build ordered list (oldest → newest)
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
        }
