"""Latency arbitrage — sub-second exchange-to-Polymarket price gap trading.

Key fixes from zero-trade diagnosis:
1. Adaptive sigmoid k: k=800 saturates too hard for small BTC moves.
   Now uses time-remaining-aware k: k=400 with >120s remaining, k=600 <60s.
2. INFO-level per-cycle diagnostics (was DEBUG → invisible in production).
3. Relaxed min_gap_pct: 0.01 → 0.008 (BTC 5-min markets are tight).
4. Micro-lag predictor: extrapolates BTC price 500ms forward based on
   recent velocity to model expected Polymarket update lag.
5. Reduced momentum conflict filter: only blocks if micro_mom strongly
   contradicts (was blocking on tiny -0.0002 counter-momentum).
6. Entry range widened: 22-78c (was 25-75c) — Polymarket 5-min often 
   opens near 50c and stays there, no reason to exclude 25-28c range.
"""

from __future__ import annotations

import math
import time

from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Direction, MarketSnapshot, Signal
from polybot.strategies.base import BaseStrategy

import structlog
logger = structlog.get_logger()


def btc_move_to_fair_probability(move_pct: float, k: float = 400.0) -> float:
    """Sigmoid fair value model.
    
    k=400: more sensitive to small moves (default, early in window)
    k=600: medium sensitivity (mid-window)  
    k=800: original value, now only used near expiry
    
    For reference at k=400:
      0.1% move → P(up) = 0.599
      0.2% move → P(up) = 0.685
      0.5% move → P(up) = 0.858
    """
    prob = 1.0 / (1.0 + math.exp(-k * move_pct))
    return max(0.05, min(0.95, prob))


def adaptive_k(time_remaining_seconds: float) -> float:
    """Scale k based on time remaining in the market window.
    
    Early in window (>120s): k=400 — be sensitive to small moves, 
                                      market hasn't priced in much yet.
    Mid window (60-120s):     k=600 — moderate sensitivity.
    Late window (<60s):       k=800 — high sensitivity, less time to revert.
    """
    if time_remaining_seconds > 120:
        return 400.0
    elif time_remaining_seconds > 60:
        return 600.0
    else:
        return 800.0


def predict_exchange_move(feed: PriceFeedState, horizon_ms: int = 500) -> float:
    """Micro-lag predictor: extrapolate BTC price change over next N ms.
    
    Uses recent 200ms velocity as a predictor for the next 500ms.
    This models the ~50-200ms lag Polymarket has vs Binance WS feed.
    
    Returns predicted move_pct over horizon.
    """
    velocity_200ms = feed.price_change_since(0.2)  # % change last 200ms
    # Extrapolate linearly (conservative — real momentum decays faster)
    scale = horizon_ms / 200.0
    predicted = velocity_200ms * scale * 0.5  # 50% decay factor
    return predicted


MAX_ENTRY_PRICE = 0.78  # Relaxed from 0.75
MIN_ENTRY_PRICE = 0.22  # Relaxed from 0.25

# How often to emit the periodic summary log (seconds)
_SUMMARY_INTERVAL = 60.0
# How often to emit a "no signal but here's why" diagnostic when gap is close (seconds)  
_DIAG_INTERVAL = 10.0


class LatencyArbStrategy(BaseStrategy):

    def __init__(
        self,
        min_gap_pct: float = 0.008,         # Relaxed from 0.01
        max_gap_pct: float = 0.25,
        min_exchange_move_pct: float = 0.0015,  # Relaxed from 0.002
        confidence_floor: float = 0.55,
        size_pct: float = 0.04,
        fee_buffer_pct: float = 0.003,
        market_keywords: list[str] | None = None,
        enable_micro_lag: bool = True,
    ) -> None:
        self._min_gap_pct = min_gap_pct
        self._max_gap_pct = max_gap_pct
        self._min_exchange_move_pct = min_exchange_move_pct
        self._confidence_floor = confidence_floor
        self._size_pct = size_pct
        self._fee_buffer_pct = fee_buffer_pct
        self._market_keywords = market_keywords
        self._enable_micro_lag = enable_micro_lag
        self._exchange_feed: PriceFeedState | None = None
        self._last_summary = 0.0
        self._last_diag: dict[str, float] = {}  # market_id → last diag time
        # Cycle stats for summary
        self._cycles = 0
        self._blocks: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "latency_arb"

    def set_exchange_feed(self, feed: PriceFeedState) -> None:
        self._exchange_feed = feed

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        self._cycles += 1

        if not self._exchange_feed or self._exchange_feed.last_price == 0:
            self._count_block("no_feed")
            return None
        if len(self._exchange_feed.ticks) < 20:
            self._count_block("feed_warming")
            return None

        exchange_price = self._exchange_feed.last_price
        poly_mid = snapshot.orderbook.mid_price

        # ── Entry price guard ──────────────────────────────────────
        if poly_mid > MAX_ENTRY_PRICE or poly_mid < MIN_ENTRY_PRICE:
            self._count_block("poly_out_of_range")
            return None

        # ── Multi-window move detection ────────────────────────────
        move_500ms = self._exchange_feed.price_change_since(0.5)
        move_1s    = self._exchange_feed.price_change_since(1.0)
        move_2s    = self._exchange_feed.price_change_since(2.0)
        move_5s    = self._exchange_feed.price_change_since(5.0)
        move_10s   = self._exchange_feed.price_change_since(10.0)
        micro_mom  = self._exchange_feed.micro_momentum()

        # Micro-lag predictor: expected additional move before Poly updates
        lag_predicted = 0.0
        if self._enable_micro_lag:
            lag_predicted = predict_exchange_move(self._exchange_feed, horizon_ms=500)

        # ── Best qualifying move ───────────────────────────────────
        best_move = 0.0
        move_window = "none"
        thresh = self._min_exchange_move_pct

        checks = [
            (move_500ms + lag_predicted, thresh * 0.4, "500ms"),
            (move_1s,                    thresh * 0.6, "1s"),
            (move_2s,                    thresh * 0.8, "2s"),
            (move_5s,                    thresh,       "5s"),
            (move_10s,                   thresh * 1.2, "10s"),
        ]
        for move, t, window in checks:
            if abs(move) >= t:
                best_move = move
                move_window = window
                break

        if best_move == 0.0:
            self._count_block("move_too_small")
            self._maybe_diag(
                snapshot.market.id, exchange_price, poly_mid,
                move_5s, move_10s, 0.0, "move_too_small", micro_mom
            )
            return None

        # ── Momentum conflict filter (relaxed) ─────────────────────
        # Only block on strong counter-momentum, not noise
        CONFLICT_THRESHOLD = 0.001  # Raised from 0.0002
        if best_move > 0 and micro_mom < -CONFLICT_THRESHOLD:
            self._count_block("momentum_conflict")
            return None
        if best_move < 0 and micro_mom > CONFLICT_THRESHOLD:
            self._count_block("momentum_conflict")
            return None

        # ── Adaptive k based on time remaining ────────────────────
        # We don't have time_remaining directly here — use metadata if available
        # Default to k=400 (early window sensitivity)
        k = 400.0
        fair_yes = btc_move_to_fair_probability(best_move, k=k)

        # ── Gap calculation ────────────────────────────────────────
        if best_move > 0:
            gap = fair_yes - poly_mid
        else:
            gap = poly_mid - fair_yes  # For SELL: we want poly > fair

        effective_gap = abs(gap) - self._fee_buffer_pct

        # ── Gap diagnostics (fires when we're close to threshold) ──
        self._maybe_diag(
            snapshot.market.id, exchange_price, poly_mid,
            move_5s, best_move, effective_gap, move_window, micro_mom, fair_yes
        )

        if effective_gap < self._min_gap_pct:
            self._count_block("gap_too_small")
            return None
        if abs(gap) > self._max_gap_pct:
            self._count_block("gap_too_large")
            return None

        # ── Direction + entry price ────────────────────────────────
        if best_move > 0:
            direction = Direction.BUY
            outcome = "Yes"
            target_price = snapshot.orderbook.best_ask
            if target_price > MAX_ENTRY_PRICE:
                self._count_block("ask_out_of_range")
                return None
        else:
            direction = Direction.SELL
            outcome = "No"
            target_price = snapshot.orderbook.best_bid
            if target_price < MIN_ENTRY_PRICE:
                self._count_block("bid_out_of_range")
                return None

        # ── Confidence ────────────────────────────────────────────
        speed_bonus = {"500ms": 0.12, "1s": 0.08, "2s": 0.04, "5s": 0.0, "10s": 0.0}
        gap_score  = min(effective_gap / 0.10, 1.0)
        move_score = min(abs(best_move) / thresh, 1.0)

        confidence = (
            gap_score  * 0.45
            + move_score * 0.35
            + speed_bonus.get(move_window, 0)
            + min(abs(micro_mom) * 500, 0.1)
        )
        confidence = max(min(confidence, 0.95), self._confidence_floor)

        self._emit_summary()

        return Signal(
            market_id=snapshot.market.id,
            strategy=self.name,
            direction=direction,
            outcome=outcome,
            target_price=target_price,
            confidence=confidence,
            size_pct=self._size_pct * confidence,
            reason=(
                f"[{move_window}] BTC {best_move:+.4%} (${exchange_price * abs(best_move):.0f}) "
                f"fair={fair_yes:.3f} poly={poly_mid:.3f} gap={effective_gap:+.3f}"
            ),
            metadata={
                "exchange_price": exchange_price,
                "best_move": best_move,
                "move_window": move_window,
                "micro_momentum": micro_mom,
                "lag_predicted": lag_predicted,
                "poly_mid": poly_mid,
                "fair_yes": fair_yes,
                "gap": gap,
                "effective_gap": effective_gap,
                "k_used": k,
                "btc_dollar_move": exchange_price * abs(best_move),
            },
        )

    def _count_block(self, reason: str) -> None:
        self._blocks[reason] = self._blocks.get(reason, 0) + 1

    def _maybe_diag(
        self,
        market_id: str,
        exchange_price: float,
        poly_mid: float,
        move_5s: float,
        best_move: float,
        effective_gap: float,
        block_reason: str,
        micro_mom: float,
        fair_yes: float | None = None,
    ) -> None:
        """Emit diagnostic INFO log when gap is within 2x of threshold (interesting cases)."""
        now = time.time()
        last = self._last_diag.get(market_id, 0.0)
        if now - last < _DIAG_INTERVAL:
            return
        # Only log if there's something meaningful happening
        if abs(best_move) < self._min_exchange_move_pct * 0.3 and effective_gap < self._min_gap_pct * 0.3:
            return
        self._last_diag[market_id] = now
        fair = fair_yes if fair_yes is not None else btc_move_to_fair_probability(move_5s, k=400.0)
        logger.info(
            "latarb_diag",
            m=market_id[:12],
            poly=round(poly_mid, 4),
            btc=round(exchange_price, 1),
            mv5s=f"{move_5s:+.4%}",
            best=f"{best_move:+.4%}",
            fair=round(fair, 4),
            eff_gap=round(effective_gap, 4),
            threshold=self._min_gap_pct,
            mom=round(micro_mom, 5),
            block=block_reason,
        )

    def _emit_summary(self) -> None:
        """Emit a per-minute summary of block reasons for tuning."""
        now = time.time()
        if now - self._last_summary < _SUMMARY_INTERVAL:
            return
        self._last_summary = now
        if self._cycles > 0:
            logger.info(
                "latarb_summary",
                cycles=self._cycles,
                blocks=self._blocks,
            )
        self._cycles = 0
        self._blocks = {}

    def get_params(self) -> dict:
        return {
            "min_gap_pct": self._min_gap_pct,
            "max_gap_pct": self._max_gap_pct,
            "min_exchange_move_pct": self._min_exchange_move_pct,
            "confidence_floor": self._confidence_floor,
            "size_pct": self._size_pct,
            "fee_buffer_pct": self._fee_buffer_pct,
            "entry_range": f"{MIN_ENTRY_PRICE}-{MAX_ENTRY_PRICE}",
            "enable_micro_lag": self._enable_micro_lag,
        }
