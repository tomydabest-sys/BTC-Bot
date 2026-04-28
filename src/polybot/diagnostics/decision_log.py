"""Structured per-cycle decision log + block-reason counter.

PATCHED FROM ORIGINAL:
1. Added 9 new BlockReason codes for diagnosing zero-trade scenarios:
   POLY_FEED_NEVER_ARRIVED, BTC_FEED_NOT_SUBSCRIBED, ORDERBOOK_TOO_LATE,
   MARKET_TOO_NEW, MID_NOT_MOVING, SIZED_TO_ZERO, KELLY_NEGATIVE,
   AGG_DROPPED_LOW_CONF, AGG_CONFLICT_ABSTAIN
2. Added FORCE_TRADE flag and relax() helper for §7D force-trade mode
3. Added stage-numbered overshoot block reasons (OVR_01..OVR_07) for
   strategy-level lifecycle visibility in analyze.py output
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
#  FORCE-TRADE MODE (§7D)
# ─────────────────────────────────────────────────────────────────────────────

FORCE_TRADE = os.environ.get("BOT_FORCE_TRADE") == "1"


def relax(value: float, factor: float = 0.5, floor: float | None = None) -> float:
    """Halve gates when in force-trade mode.

    Usage in strategies:
        if abs(poly_burst) < relax(self._min_poly_burst, 0.5, floor=0.001):
            return self._block(BlockReason.NO_BURST, ...)
    """
    if not FORCE_TRADE:
        return value
    out = value * factor
    return max(out, floor) if floor is not None else out


# ─────────────────────────────────────────────────────────────────────────────
#  BlockReason — canonical decision-log reason codes
# ─────────────────────────────────────────────────────────────────────────────


class BlockReason:
    """Canonical reasons for every decision-pipeline outcome."""

    OK = "ok"
    NO_SIGNAL = "no_signal"

    # ── Threshold gates ──
    BELOW_MIN_EDGE = "below_min_edge"
    BELOW_MIN_CONFIDENCE = "below_min_confidence"
    BELOW_MIN_NET_SIGNAL = "below_min_net_signal"
    NO_BURST = "no_burst"
    OVERSHOOT_TOO_SMALL = "overshoot_too_small"
    OVERSHOOT_TOO_LARGE = "overshoot_too_large"
    BTC_MOVE_TOO_SMALL = "btc_move_too_small"
    BTC_MOVE_TOO_LARGE = "btc_move_too_large"
    OUTSIDE_PRICE_BAND = "outside_price_band"
    SPREAD_TOO_WIDE = "spread_too_wide"
    SPREAD_TOO_NARROW = "spread_too_narrow"
    FEE_EXCEEDS_EDGE = "fee_exceeds_edge"

    # ── Time gates ──
    TIME_REMAINING_TOO_LOW = "time_remaining_too_low"
    TIME_REMAINING_TOO_HIGH = "time_remaining_too_high"

    # ── Execution gates ──
    COOLDOWN = "cooldown"
    RISK_BLOCK = "risk_block"
    POSITION_CAP = "position_cap"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    KILL_SWITCH = "kill_switch"
    DAILY_LOSS_HALT = "daily_loss_halt"

    # ── Data quality gates ──
    STALE_ORDERBOOK = "stale_orderbook"
    EMPTY_ORDERBOOK = "empty_orderbook"
    FEED_WARMING = "feed_warming"
    FEED_STALE = "feed_stale"
    INVALID_BOOK = "invalid_book"
    NO_FEED = "no_feed"

    # ── Direction / consistency gates ──
    WRONG_DIRECTION = "wrong_direction"
    DIRECTION_CONFLICT = "direction_conflict"
    MOMENTUM_CONFLICT = "momentum_conflict"
    BURST_NOT_BTC_DRIVEN = "burst_not_btc_driven"

    # ── Misc ──
    AGGREGATOR_DROPPED = "aggregator_dropped"
    BTC_VOL_ZERO = "btc_vol_zero"
    BTC_FEED_INVALID = "btc_feed_invalid"

    # ── NEW: Data freshness diagnostics ──
    POLY_FEED_NEVER_ARRIVED = "poly_feed_never_arrived"
    BTC_FEED_NOT_SUBSCRIBED = "btc_feed_not_subscribed"
    ORDERBOOK_TOO_LATE = "orderbook_too_late"
    MARKET_TOO_NEW = "market_too_new"
    MID_NOT_MOVING = "mid_not_moving"

    # ── NEW: Sizing diagnostics ──
    SIZED_TO_ZERO = "sized_to_zero"
    KELLY_NEGATIVE = "kelly_negative"

    # ── NEW: Aggregator-specific (split out from generic LOW_CONFIDENCE) ──
    AGG_DROPPED_LOW_CONF = "agg_dropped_low_conf"
    AGG_CONFLICT_ABSTAIN = "agg_conflict_abstain"

    # ── NEW: Stage-numbered overshoot lifecycle (sortable in analyze.py) ──
    OVR_01_FEED_WARMING = "ovr_01_feed_warming"
    OVR_02_TIME_RANGE = "ovr_02_time_range"
    OVR_03_BOOK_INVALID = "ovr_03_book_invalid"
    OVR_04_BTC_MOVE = "ovr_04_btc_move"
    OVR_05_NO_BURST = "ovr_05_no_burst"
    OVR_06_DIRECTION = "ovr_06_direction"
    OVR_07_OVERSHOOT_RANGE = "ovr_07_overshoot_range"
    OVR_08_EDGE = "ovr_08_edge"


# Process-wide block-reason counter
block_counter: Counter[str] = Counter()


# ─────────────────────────────────────────────────────────────────────────────
#  File-based JSONL logger
# ─────────────────────────────────────────────────────────────────────────────


class _DecisionLogger:
    """Writes one JSON line per emit() call with daily rotation."""

    def __init__(
        self,
        path: str = "logs/decisions.jsonl",
        flush_every: int = 25,
    ) -> None:
        self._path = path
        self._flush_every = flush_every
        self._fh = None
        self._writes_since_flush = 0
        self._current_date: str = ""
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def _maybe_rotate(self) -> None:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._current_date:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                except Exception:
                    pass
                self._fh = None
            self._current_date = today
            base = Path(self._path)
            if base.exists() and self._current_date:
                rotated = base.with_name(f"{base.stem}.{self._current_date}{base.suffix}")
                if not rotated.exists():
                    try:
                        import os as _os
                        from datetime import timezone as _tz
                        mtime = datetime.fromtimestamp(_os.path.getmtime(base), tz=_tz.utc)
                        midnight = datetime.utcnow().replace(
                            hour=0, minute=0, second=0, microsecond=0, tzinfo=_tz.utc
                        )
                        if mtime < midnight:
                            yday = mtime.strftime("%Y-%m-%d")
                            rotated_y = base.with_name(f"{base.stem}.{yday}{base.suffix}")
                            if not rotated_y.exists():
                                base.rename(rotated_y)
                    except Exception:
                        pass

    def _open(self):
        self._maybe_rotate()
        if self._fh is None:
            self._fh = open(self._path, "a", encoding="utf-8", buffering=1)
        return self._fh

    def write(self, payload: dict) -> None:
        try:
            fh = self._open()
            fh.write(json.dumps(payload, default=_json_default) + "\n")
            self._writes_since_flush += 1
            if self._writes_since_flush >= self._flush_every:
                fh.flush()
                self._writes_since_flush = 0
        except Exception:
            pass

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
            self._fh = None


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


_LOG_PATH = os.environ.get("BTC_BOT_DECISION_LOG", "logs/decisions.jsonl")
_logger = _DecisionLogger(path=_LOG_PATH)


def configure(path: str | None = None, flush_every: int = 25) -> None:
    global _logger
    _logger.close()
    _logger = _DecisionLogger(
        path=path or _LOG_PATH,
        flush_every=flush_every,
    )


def shutdown() -> None:
    _logger.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Public emit() API
# ─────────────────────────────────────────────────────────────────────────────


def emit(
    *,
    cycle_id: str = "",
    strategy: str = "",
    market_id: str = "",
    timeframe: str = "",
    binance_px: float = 0.0,
    fair_value: float = 0.0,
    mid: float = 0.0,
    best_bid: float = 0.0,
    best_ask: float = 0.0,
    spread_bps: float = 0.0,
    edge_bps: float = 0.0,
    confidence: float = 0.0,
    decision: str = "NO_SIGNAL",
    reason: str = BlockReason.NO_SIGNAL,
    time_to_expiry_s: float = 0.0,
    ob_age_ms: float = 0.0,
    btc_move_5s: float = 0.0,
    btc_move_30s: float = 0.0,
    btc_move_60s: float = 0.0,
    poly_burst_5s: float = 0.0,
    size_usd: float = 0.0,
    kelly_f: float = 0.0,
    extra: dict | None = None,
) -> None:
    """Emit one decision-log line."""
    block_counter[reason] += 1
    payload = {
        "ts": time.time(),
        "iso": datetime.utcnow().isoformat() + "Z",
        "cycle_id": cycle_id,
        "strategy": strategy,
        "market_id": market_id[:32] if market_id else "",
        "timeframe": timeframe,
        "binance_px": _round(binance_px, 2),
        "fair_value": _round(fair_value, 5),
        "mid": _round(mid, 5),
        "best_bid": _round(best_bid, 5),
        "best_ask": _round(best_ask, 5),
        "spread_bps": _round(spread_bps, 2),
        "edge_bps": _round(edge_bps, 2),
        "confidence": _round(confidence, 4),
        "decision": decision,
        "reason": reason,
        "time_to_expiry_s": _round(time_to_expiry_s, 1),
        "ob_age_ms": _round(ob_age_ms, 0),
        "btc_move_5s": _round(btc_move_5s, 6),
        "btc_move_30s": _round(btc_move_30s, 6),
        "btc_move_60s": _round(btc_move_60s, 6),
        "poly_burst_5s": _round(poly_burst_5s, 5),
        "size_usd": _round(size_usd, 2),
        "kelly_f": _round(kelly_f, 4),
        "force_trade": FORCE_TRADE,
    }
    if extra:
        for k, v in extra.items():
            if k not in payload:
                payload[k] = v
    _logger.write(payload)


def _round(v: float, digits: int) -> float:
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return 0.0


def block_summary(top_n: int = 15) -> dict[str, int]:
    return dict(block_counter.most_common(top_n))


def reset_counter() -> None:
    block_counter.clear()
