"""Momentum lag strategy — exchange momentum with Polymarket price lag detection.

Fixes from zero-trade diagnosis:
1. Thresholds relaxed: min_move_30s 0.2%→0.15%, min_move_60s 0.4%→0.25%.
   BTC moved ~$100-250 during the dead period (~0.15-0.37%) — previous
   thresholds were too high for ranging conditions.
2. Entry range widened to 22-78c (was 25-75c).
3. INFO-level diagnostics every 10s when gap is interesting (was DEBUG).
4. Removed thin-book requirement for short-window moves — in tight ranging
   markets the book can be thick AND have a gap.
5. Directional consistency check relaxed: 30s * 60s signs must agree,
   but now allows 60s=0 (flat) if 30s is strong.
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
    prob = 1.0 / (1.0 + math.exp(-k * move_pct))
    return max(0.05, min(0.95, prob))


MAX_ENTRY_PRICE = 0.78
MIN_ENTRY_PRICE = 0.22

_DIAG_INTERVAL = 10.0
_SUMMARY_INTERVAL = 60.0


class MomentumLagStrategy(BaseStrategy):

    def __init__(
        self,
        min_move_30s_pct: float = 0.0015,   # Relaxed from 0.002
        min_move_60s_pct: float = 0.0025,   # Relaxed from 0.004
        min_gap_pct: float = 0.008,          # Relaxed from 0.01
        max_gap_pct: float = 0.20,
        thin_book_threshold: float = 0.01,
        size_pct: float = 0.04,
        market_keywords: list[str] | None = None,
    ) -> None:
        self._min_move_30s = min_move_30s_pct
        self._min_move_60s = min_move_60s_pct
        self._min_gap_pct = min_gap_pct
        self._max_gap_pct = max_gap_pct
        self._thin_book_threshold = thin_book_threshold
        self._size_pct = size_pct
        self._market_keywords = market_keywords
        self._exchange_feed: PriceFeedState | None = None
        self._last_diag: dict[str, float] = {}
        self._last_summary = 0.0
        self._cycles = 0
        self._blocks: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "momentum_lag"

    def set_exchange_feed(self, feed: PriceFeedState) -> None:
        self._exchange_feed = feed

    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        self._cycles += 1

        if not self._exchange_feed or len(self._exchange_feed.ticks) < 30:
            self._count_block("feed_warming")
            return None

        poly_mid = snapshot.orderbook.mid_price
        if poly_mid > MAX_ENTRY_PRICE or poly_mid < MIN_ENTRY_PRICE:
            self._count_block("poly_out_of_range")
            return None

        move_30s = self._exchange_feed.price_change_pct(30)
        move_60s = self._exchange_feed.price_change_pct(60)

        strong_30s = abs(move_30s) >= self._min_move_30s
        strong_60s = abs(move_60s) >= self._min_move_60s

        if not (strong_30s or strong_60s):
            self._count_block("move_too_small")
            self._maybe_diag(snapshot.market.id, poly_mid, move_30s, move_60s, 0.0, "move_too_small")
            return None

        # Directional consistency — allow 60s=near-zero if 30s is strong
        if abs(move_60s) > self._min_move_60s * 0.5:
            if move_30s * move_60s < 0:
                self._count_block("direction_conflict")
                return None

        move_pct = move_30s if strong_30s else move_60s

        spread = snapshot.orderbook.spread
        is_thin = spread >= self._thin_book_threshold

        # RELAXED: no longer require thin book — just require minimum gap
        # (thick book with gap is still tradeable)

        # Adaptive k based on momentum strength
        k = 400.0 if abs(move_pct) < 0.003 else 600.0

        fair_yes = btc_move_to_fair_probability(move_pct, k=k)

        if move_pct > 0:
            gap = fair_yes - poly_mid
        else:
            gap = poly_mid - fair_yes

        self._maybe_diag(snapshot.market.id, poly_mid, move_30s, move_60s, gap, "evaluating", fair_yes)

        if gap < self._min_gap_pct:
            self._count_block("gap_too_small")
            return None
        if gap > self._max_gap_pct:
            self._count_block("gap_too_large")
            return None

        if move_pct > 0:
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

        move_score = min(abs(move_pct) / (self._min_move_60s * 3), 1.0)
        gap_score  = min(gap / 0.08, 1.0)
        thin_bonus = 0.08 if is_thin else 0.0
        confidence = min(move_score * 0.45 + gap_score * 0.4 + thin_bonus, 0.95)

        exchange_price = self._exchange_feed.last_price
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
                f"Momentum {move_30s:+.3%}(30s) {move_60s:+.3%}(60s) "
                f"(${exchange_price * abs(move_pct):.0f}) "
                f"fair={fair_yes:.3f} poly={poly_mid:.3f} gap={gap:.3f}"
            ),
            metadata={
                "move_30s": move_30s,
                "move_60s": move_60s,
                "fair_yes": fair_yes,
                "gap": gap,
                "spread": spread,
                "is_thin_book": is_thin,
                "exchange_price": exchange_price,
                "k_used": k,
            },
        )

    def _count_block(self, reason: str) -> None:
        self._blocks[reason] = self._blocks.get(reason, 0) + 1

    def _maybe_diag(
        self,
        market_id: str,
        poly_mid: float,
        move_30s: float,
        move_60s: float,
        gap: float,
        block_reason: str,
        fair_yes: float | None = None,
    ) -> None:
        """Emit INFO diagnostic when gap is > 30% of threshold (interesting)."""
        now = time.time()
        last = self._last_diag.get(market_id, 0.0)
        if now - last < _DIAG_INTERVAL:
            return
        # Only if something interesting is happening
        if abs(move_30s) < self._min_move_30s * 0.3 and abs(move_60s) < self._min_move_60s * 0.3:
            return
        self._last_diag[market_id] = now
        feed = self._exchange_feed
        fair = fair_yes if fair_yes is not None else btc_move_to_fair_probability(move_30s, k=400.0)
        logger.info(
            "momlag_diag",
            m=market_id[:12],
            poly=round(poly_mid, 4),
            btc=round(feed.last_price if feed else 0, 1),
            mv30s=f"{move_30s:+.4%}",
            mv60s=f"{move_60s:+.4%}",
            fair=round(fair, 4),
            gap=round(gap, 4),
            threshold=self._min_gap_pct,
            block=block_reason,
        )

    def _emit_summary(self) -> None:
        now = time.time()
        if now - self._last_summary < _SUMMARY_INTERVAL:
            return
        self._last_summary = now
        if self._cycles > 0:
            logger.info(
                "momlag_summary",
                cycles=self._cycles,
                blocks=self._blocks,
            )
        self._cycles = 0
        self._blocks = {}

    def get_params(self) -> dict:
        return {
            "min_move_30s_pct": self._min_move_30s,
            "min_move_60s_pct": self._min_move_60s,
            "min_gap_pct": self._min_gap_pct,
            "max_gap_pct": self._max_gap_pct,
            "thin_book_threshold": self._thin_book_threshold,
            "size_pct": self._size_pct,
            "entry_range": f"{MIN_ENTRY_PRICE}-{MAX_ENTRY_PRICE}",
        }
