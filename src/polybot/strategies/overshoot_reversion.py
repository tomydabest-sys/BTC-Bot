"""Overshoot Reversion — fade post-burst Polymarket overextension.

Hypothesis: competing latency-arb bots create temporary impact on Polymarket
after sharp BTC moves. The aggregate impact pushes Polymarket past the rational
GBM fair value. Over 30–120s, flow normalizes and Polymarket retraces 30–60%
of the overshoot.

Signal:
  burst    = |poly_move_5s| >= min_poly_burst
  fair     = 1 - Phi(-btc_move_window / (btc_vol_per_s * sqrt(t_rem)))
  overshoot= poly_mid - fair    (same sign as the burst)
  enter    = burst AND |overshoot| >= min_overshoot AND t_rem >= min_t_rem
  side     = -sign(overshoot)   (fade)

Exits (handled in PositionManager for this strategy):
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
from polybot.strategies.base import BaseStrategy

logger = structlog.get_logger()


def normal_cdf(x: float) -> float:
    """Standard normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def gbm_fair_probability(
    move_pct: float, t_rem_seconds: float, vol_per_second: float
) -> float:
    """P(settle above current) under GBM with realized vol.

    Returns 0.5 on degenerate input. Clamped to [0.02, 0.98].
    """
    if t_rem_seconds <= 0 or vol_per_second <= 0:
        return 0.5
    sigma_rem = vol_per_second * math.sqrt(t_rem_seconds)
    if sigma_rem <= 0:
        return 0.5
    z = -move_pct / sigma_rem
    p = 1.0 - normal_cdf(z)
    return max(0.02, min(0.98, p))


_DIAG_INTERVAL = 10.0
_SUMMARY_INTERVAL = 60.0


class OvershootReversionStrategy(BaseStrategy):
    """Fades Polymarket bursts that exceed GBM-implied fair value."""

    def __init__(
        self,
        min_poly_burst: float = 0.025,
        min_overshoot: float = 0.020,
        max_overshoot: float = 0.120,
        min_time_remaining: float = 90.0,
        min_poly_mid: float = 0.25,
        max_poly_mid: float = 0.75,
        max_spread: float = 0.030,
        min_btc_move_confirm: float = 0.0005,
        size_pct: float = 0.05,
        confidence_floor: float = 0.55,
        window_seconds: float = 300.0,
    ) -> None:
        self._min_poly_burst = float(min_poly_burst)
        self._min_overshoot = float(min_overshoot)
        self._max_overshoot = float(max_overshoot)
        self._min_t_rem = float(min_time_remaining)
        self._min_mid = float(min_poly_mid)
        self._max_mid = float(max_poly_mid)
        self._max_spread = float(max_spread)
        self._min_btc_move = float(min_btc_move_confirm)
        self._size_pct = float(size_pct)
        self._confidence_floor = float(confidence_floor)
        self._window_seconds = float(window_seconds)
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

        if self._feed is None or len(self._feed.ticks) < 60:
            self._count_block("feed_warming")
            return None

        # Time remaining
        now = datetime.utcnow()
        end = snapshot.market.end_date
        if getattr(end, "tzinfo", None) is not None:
            end = end.replace(tzinfo=None)
        t_rem = (end - now).total_seconds()
        if t_rem < self._min_t_rem:
            self._count_block("t_rem_too_short")
            return None

        ob = snapshot.orderbook
        mid = ob.mid_price

        if not (self._min_mid < mid < self._max_mid):
            self._count_block("poly_out_of_range")
            return None

        if ob.spread > self._max_spread:
            self._count_block("spread_too_wide")
            return None

        if ob.best_bid <= 0 or ob.best_ask >= 1.0:
            self._count_block("invalid_book")
            return None

        poly_move_5s = float(getattr(snapshot, "poly_move_5s", 0.0) or 0.0)
        if abs(poly_move_5s) < self._min_poly_burst:
            self._count_block("no_burst")
            return None

        btc_move_10s = self._feed.price_change_since(10.0)
        if abs(btc_move_10s) < self._min_btc_move:
            self._count_block("btc_move_too_small")
            return None

        if (poly_move_5s > 0) != (btc_move_10s > 0):
            self._count_block("burst_not_btc_driven")
            return None

        # BTC move since window open (capped at window_seconds)
        elapsed_in_window = max(5.0, min(self._window_seconds, self._window_seconds - t_rem))
        btc_move_window = self._feed.price_change_since(elapsed_in_window)

        btc_last = self._feed.last_price
        if btc_last <= 0:
            self._count_block("btc_feed_invalid")
            return None

        btc_rv_60 = self._feed.volatility_window(60)
        if btc_rv_60 <= 0:
            self._count_block("btc_vol_zero")
            return None
        # volatility_window returns absolute price stdev over the window;
        # convert to per-second return vol
        btc_vol_per_s = (btc_rv_60 / btc_last) / math.sqrt(60.0)
        if btc_vol_per_s <= 0:
            self._count_block("btc_vol_zero")
            return None

        fair = gbm_fair_probability(btc_move_window, t_rem, btc_vol_per_s)
        overshoot = mid - fair

        # Overshoot must be same sign as the burst; otherwise the burst already moved
        # toward fair value (no reversion setup).
        if (overshoot > 0) != (poly_move_5s > 0):
            self._count_block("wrong_direction")
            return None

        abs_os = abs(overshoot)
        if abs_os < self._min_overshoot:
            self._count_block("overshoot_too_small")
            self._maybe_diag(
                snapshot.market.id, mid, fair, poly_move_5s, btc_move_window,
                overshoot, t_rem, "overshoot_too_small",
            )
            return None
        if abs_os > self._max_overshoot:
            self._count_block("overshoot_too_large")
            return None

        # FADE the overshoot
        if overshoot > 0:
            # Market too high → SELL No at best_bid
            direction = Direction.SELL
            outcome = "No"
            target_price = ob.best_bid
            if target_price < self._min_mid:
                self._count_block("bid_out_of_range")
                return None
        else:
            # Market too low → BUY Yes at best_ask
            direction = Direction.BUY
            outcome = "Yes"
            target_price = ob.best_ask
            if target_price > self._max_mid:
                self._count_block("ask_out_of_range")
                return None

        # Confidence
        burst_score = min(abs(poly_move_5s) / 0.06, 1.0)
        overshoot_score = min(abs_os / 0.08, 1.0)
        time_score = min(t_rem / 180.0, 1.0)
        confidence = 0.30 + 0.35 * burst_score + 0.25 * overshoot_score + 0.10 * time_score
        confidence = max(self._confidence_floor, min(confidence, 0.92))

        reason = (
            f"burst={poly_move_5s:+.3f} btc10s={btc_move_10s:+.4%} "
            f"fair={fair:.3f} mid={mid:.3f} os={overshoot:+.3f} "
            f"t_rem={t_rem:.0f}s"
        )

        logger.info(
            "overshoot_signal",
            m=snapshot.market.id[:12],
            dir=direction.value,
            mid=round(mid, 4),
            fair=round(fair, 4),
            os=round(overshoot, 4),
            burst=round(poly_move_5s, 4),
            btc10s=round(btc_move_10s, 5),
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
            size_pct=self._size_pct * confidence,
            reason=reason,
            metadata={
                "poly_move_5s": poly_move_5s,
                "btc_move_10s": btc_move_10s,
                "btc_move_window": btc_move_window,
                "btc_vol_per_s": btc_vol_per_s,
                "gbm_fair": fair,
                "overshoot": overshoot,
                "t_rem": t_rem,
                "entry_mid": mid,
                "is_fade": True,
            },
        )

    def get_params(self) -> dict:
        return {
            "min_poly_burst": self._min_poly_burst,
            "min_overshoot": self._min_overshoot,
            "max_overshoot": self._max_overshoot,
            "min_time_remaining": self._min_t_rem,
            "entry_range": f"{self._min_mid}-{self._max_mid}",
            "max_spread": self._max_spread,
            "min_btc_move_confirm": self._min_btc_move,
            "size_pct": self._size_pct,
            "confidence_floor": self._confidence_floor,
            "window_seconds": self._window_seconds,
        }

    # ── Internals ────────────────────────────────────────────────────

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
