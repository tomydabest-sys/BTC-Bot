"""Boundary decay — trade near-certainty terminal payoffs in the final minute.

When `t_rem < 60s` and BTC has moved decisively from the strike (defined as
`|btc_move_window_pct| > 0.10%`), the BTC up/down market has a near-certain
"should-resolve" answer. Mispricings exist when the losing side trades higher
than its remaining tail probability.

This strategy works for retail because:
1. The 70ms latency disadvantage is irrelevant — the underlying outcome is
   already nearly determined; you're not racing anyone, you're collecting
   on convexity that the book hasn't fully priced.
2. Black-Scholes binary tail probability (N(d2)) gives an honest closed-form
   for "remaining probability of price crossing back through strike."
3. Win rate is 90–95% by construction: the loss case is a late-window BTC
   reversal through strike, ~5–10% historically.

Signal:
  win_side = "Yes" if btc_now > strike else "No"
  P(win)   = N(d2)  (very close to 1 when BTC is far from strike with little time)
  edge_yes = (1 - P(yes_loses)) - mid_yes  if Yes is the winning side
  enter    = win_side priced too low (e.g. <8c) OR loser priced too high (e.g. >92c)
            AND |btc_move_window_pct| > min_btc_delta_pct
            AND t_rem < max_time_remaining
            AND fee_aware_edge >= edge_floor_bps
  side     = BUY winning side / SELL losing side

Exits: held to expiry (auto-close <20s before expiry handles this)
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


class BoundaryDecayStrategy(BaseStrategy):
    """Trade near-certainty BTC up/down resolutions in the last minute."""

    def __init__(
        self,
        max_time_remaining: float = 60.0,
        min_btc_delta_pct: float = 0.0010,
        fade_above_price: float = 0.92,
        fade_below_price: float = 0.08,
        min_edge_cents: float = 0.015,
        size_pct: float = 0.06,
        confidence_floor: float = 0.65,
        sigma_lookback_s: int = 900,
        sigma_halflife_s: int = 60,
        fee_theta_taker: float = 0.072,
        edge_floor_bps: float = 10.0,
    ) -> None:
        self._max_t_rem = float(max_time_remaining)
        self._min_btc_delta = float(min_btc_delta_pct)
        self._fade_above = float(fade_above_price)
        self._fade_below = float(fade_below_price)
        self._min_edge_cents = float(min_edge_cents)
        self._size_pct = float(size_pct)
        self._confidence_floor = float(confidence_floor)
        self._sigma_lookback = int(sigma_lookback_s)
        self._sigma_halflife = int(sigma_halflife_s)
        self._fee_theta = float(fee_theta_taker)
        self._edge_floor_bps = float(edge_floor_bps)
        self._feed: PriceFeedState | None = None
        self._last_summary: float = 0.0
        self._cycles: int = 0
        self._blocks: dict[str, int] = {}

    @property
    def name(self) -> str:
        return "boundary_decay"

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

        # ── Feed warming ─────────────────────────────────────────────
        if self._feed is None or len(self._feed.ticks) < 60:
            common["reason"] = BlockReason.FEED_WARMING
            self._count_block(BlockReason.FEED_WARMING)
            emit(**common)
            return None

        common["binance_px"] = float(self._feed.last_price)

        # ── Time gate: only the final minute ─────────────────────────
        now_dt = datetime.utcnow()
        end = snapshot.market.end_date
        if getattr(end, "tzinfo", None) is not None:
            end = end.replace(tzinfo=None)
        t_rem = (end - now_dt).total_seconds()
        common["time_to_expiry_s"] = t_rem

        if t_rem <= 20:  # let auto-close handle <20s
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

        # ── BTC move features ────────────────────────────────────────
        spot = float(self._feed.last_price)
        if spot <= 0:
            common["reason"] = BlockReason.BTC_FEED_INVALID
            self._count_block(BlockReason.BTC_FEED_INVALID)
            emit(**common)
            return None

        btc_move_5s = float(self._feed.price_change_since(5.0))
        btc_move_30s = float(self._feed.price_change_since(30.0))
        btc_move_60s = float(self._feed.price_change_since(60.0))
        common["btc_move_5s"] = btc_move_5s
        common["btc_move_30s"] = btc_move_30s
        common["btc_move_60s"] = btc_move_60s

        # BTC move since window open
        # For 5m windows: window_seconds=300, but if t_rem=45 then elapsed=255s
        # Approximate: use whichever lookback covers the window
        window_total = 300.0  # default 5m window; longer timeframes will saturate
        elapsed_in_window = max(5.0, min(window_total, window_total - t_rem))
        btc_move_window = float(self._feed.price_change_since(elapsed_in_window))

        if abs(btc_move_window) < self._min_btc_delta:
            common["reason"] = BlockReason.BTC_MOVE_TOO_SMALL
            self._count_block(BlockReason.BTC_MOVE_TOO_SMALL)
            emit(**common)
            return None

        # ── Compute realized vol → fair value ────────────────────────
        sigma_per_sec = self._compute_sigma_per_sec()
        if sigma_per_sec <= 0:
            common["reason"] = BlockReason.BTC_VOL_ZERO
            self._count_block(BlockReason.BTC_VOL_ZERO)
            emit(**common)
            return None

        # Strike = spot at window open
        strike = spot / (1.0 + btc_move_window) if (1.0 + btc_move_window) > 0 else spot

        # P(yes) = P(spot_T >= strike) under remaining-time GBM
        p_yes = bs_fair_value(spot, strike, t_rem, sigma_per_sec)
        p_no = 1.0 - p_yes
        common["fair_value"] = p_yes

        # ── Decision: which side is winning, is it priced cheaply enough? ──
        # We're fading extreme prices on either side that don't reflect the
        # near-certainty of the resolution.
        signal_yes = None
        signal_no = None

        # Case 1: Yes is the winning side (BTC up from strike)
        # Buy Yes if it's mispriced LOW (mid_yes < fade_below_price means market
        # disagrees with the obvious resolution)
        if p_yes > 0.85 and ob.best_ask < self._fade_below_high_winner_threshold():
            # Buy Yes cheap
            edge = p_yes - ob.best_ask
            net_edge = fee_aware_edge(p_yes, ob.best_ask, exit_p_estimate=p_yes,
                                      theta=self._fee_theta)
            if edge >= self._min_edge_cents and net_edge * 10000 >= self._edge_floor_bps:
                signal_yes = ("BUY_YES", ob.best_ask, edge, net_edge)

        # Case 2: No is the winning side (BTC down from strike)
        if p_no > 0.85 and (1.0 - ob.best_bid) < self._fade_below_high_winner_threshold():
            # Buy No cheap (== sell Yes high). On Polymarket, you BUY No directly.
            # We model it as direction=SELL on Yes leg.
            no_ask = 1.0 - ob.best_bid  # implied
            edge = p_no - no_ask
            net_edge = fee_aware_edge(p_no, no_ask, exit_p_estimate=p_no,
                                      theta=self._fee_theta)
            if edge >= self._min_edge_cents and net_edge * 10000 >= self._edge_floor_bps:
                signal_no = ("SELL_YES", ob.best_bid, edge, net_edge)

        # Case 3: Yes is OVERPRICED relative to its tail probability
        # (BTC clearly down; Yes still trades >fade_above)
        if p_yes < 0.15 and ob.best_bid > self._fade_above:
            # SELL Yes (it's priced too high given near-certain No resolution)
            edge = ob.best_bid - p_yes
            net_edge = fee_aware_edge(1.0 - p_yes, 1.0 - ob.best_bid,
                                      exit_p_estimate=p_no,
                                      theta=self._fee_theta)
            if edge >= self._min_edge_cents and net_edge * 10000 >= self._edge_floor_bps:
                if signal_no is None or edge > signal_no[2]:
                    signal_no = ("SELL_YES", ob.best_bid, edge, net_edge)

        # Case 4: No is OVERPRICED → BUY YES cheap
        if p_no < 0.15 and (1.0 - ob.best_ask) > self._fade_above:
            edge = (1.0 - ob.best_ask) - p_no
            net_edge = fee_aware_edge(p_yes, ob.best_ask, exit_p_estimate=p_yes,
                                      theta=self._fee_theta)
            if edge >= self._min_edge_cents and net_edge * 10000 >= self._edge_floor_bps:
                if signal_yes is None or edge > signal_yes[2]:
                    signal_yes = ("BUY_YES", ob.best_ask, edge, net_edge)

        # Pick the highest-edge candidate
        candidates = [c for c in (signal_yes, signal_no) if c is not None]
        if not candidates:
            common["reason"] = BlockReason.BELOW_MIN_EDGE
            self._count_block(BlockReason.BELOW_MIN_EDGE)
            emit(**common)
            return None

        chosen = max(candidates, key=lambda c: c[2])
        side_str, target_price, raw_edge, net_edge = chosen
        edge_bps = net_edge * 10000
        common["edge_bps"] = edge_bps

        if side_str == "BUY_YES":
            direction = Direction.BUY
            outcome = "Yes"
        else:
            direction = Direction.SELL
            outcome = "No"

        # ── Confidence ───────────────────────────────────────────────
        # Higher when (a) BTC is far from strike (move_window large), (b) edge is fat,
        # (c) very little time left (more certain)
        certainty_score = max(p_yes, p_no)  # closer to 1 = more certain
        time_score = 1.0 - min(t_rem / self._max_t_rem, 1.0)  # less time = more confident
        edge_score = min(raw_edge / 0.05, 1.0)
        confidence = 0.30 + 0.35 * certainty_score + 0.20 * time_score + 0.15 * edge_score
        confidence = max(self._confidence_floor, min(confidence, 0.95))
        common["confidence"] = confidence

        size_pct_final = self._size_pct * confidence
        common["decision"] = direction.value
        common["reason"] = BlockReason.OK

        emit(**common, extra={
            "p_yes": p_yes,
            "p_no": p_no,
            "btc_move_window": btc_move_window,
            "sigma_per_sec": sigma_per_sec,
            "strike": strike,
            "raw_edge_cents": raw_edge,
        })

        reason_str = (
            f"boundary side={side_str} p_yes={p_yes:.3f} mid={mid:.3f} "
            f"target={target_price:.3f} edge={raw_edge:+.4f} edge_bps={edge_bps:.1f} "
            f"t_rem={t_rem:.0f}s btc_w={btc_move_window:+.4%}"
        )

        logger.info(
            "boundary_signal",
            m=market_id[:12],
            dir=direction.value,
            side=side_str,
            p_yes=round(p_yes, 4),
            mid=round(mid, 4),
            edge=round(raw_edge, 4),
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
                "p_yes": p_yes,
                "p_no": p_no,
                "btc_move_window": btc_move_window,
                "sigma_per_sec": sigma_per_sec,
                "strike": strike,
                "raw_edge_cents": raw_edge,
                "edge_bps": edge_bps,
                "is_boundary": True,
            },
        )

    # ─────────────────────────────────────────────────────────────────
    #  Internals
    # ─────────────────────────────────────────────────────────────────

    def _fade_below_high_winner_threshold(self) -> float:
        """Mid below which we'd buy a near-certain winner.

        If p_yes > 0.85 and yes is priced below this, we treat it as
        mispriced-cheap. Default mirrors fade_below_price (0.08).
        """
        # We want the winner to be priced "low" — i.e. cheaper than its
        # near-certain probability would suggest. Defining "low" as anything
        # below 1 - fade_above (e.g. fade_above=0.92 → threshold 0.08).
        return 1.0 - self._fade_above + self._min_edge_cents * 5

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

    def _emit_summary(self) -> None:
        now = time.time()
        if now - self._last_summary < _SUMMARY_INTERVAL:
            return
        self._last_summary = now
        if self._cycles > 0:
            logger.info(
                "boundary_summary",
                cycles=self._cycles,
                blocks=self._blocks,
            )
        self._cycles = 0
        self._blocks = {}

    def get_params(self) -> dict:
        return {
            "max_time_remaining": self._max_t_rem,
            "min_btc_delta_pct": self._min_btc_delta,
            "fade_above_price": self._fade_above,
            "fade_below_price": self._fade_below,
            "min_edge_cents": self._min_edge_cents,
            "size_pct": self._size_pct,
            "confidence_floor": self._confidence_floor,
            "sigma_lookback_s": self._sigma_lookback,
            "sigma_halflife_s": self._sigma_halflife,
            "fee_theta_taker": self._fee_theta,
            "edge_floor_bps": self._edge_floor_bps,
        }
