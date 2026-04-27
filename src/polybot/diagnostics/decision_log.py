"""Structured per-cycle decision log + block-reason counter.

This is the highest-leverage diagnostic change in the bot. Every strategy's
evaluate() call must end with a call to emit(), even when no signal is produced.
This gives you a single JSONL stream where every cycle has exactly one row per
(strategy, market) pair, with a canonical reason for every decision.

Usage from a strategy:
    from polybot.diagnostics.decision_log import emit, BlockReason, block_counter

    emit(
        cycle_id=cycle_id,
        strategy=self.name,
        market_id=market.id,
        timeframe="5m",
        binance_px=binance_price,
        fair_value=fair,
        mid=mid,
        best_bid=ob.best_bid,
        best_ask=ob.best_ask,
        spread_bps=ob.spread * 10000,
        edge_bps=edge * 10000,
        confidence=confidence,
        decision="BUY",  # or "SELL", "BLOCKED", "NO_SIGNAL"
        reason=BlockReason.OK,  # or any other BlockReason
        time_to_expiry_s=t_rem,
        ob_age_ms=ob_age_ms,
        btc_move_5s=btc_move_5s,
        btc_move_30s=btc_move_30s,
        btc_move_60s=btc_move_60s,
        poly_burst_5s=poly_burst,
        size_usd=size_usd,
        kelly_f=kelly_f,
    )

Read with:
    import pandas as pd
    df = pd.read_json("logs/decisions.jsonl", lines=True)
    print(df[df.decision != "BUY"].reason.value_counts(normalize=True))
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


class BlockReason:
    """Canonical reasons for every decision-pipeline outcome.

    Every emit() call must use exactly one of these strings. This makes the
    block_counter histogram unambiguous and the value_counts() output a
    single-glance diagnostic.
    """

    OK = "ok"
    NO_SIGNAL = "no_signal"

    # Threshold gates
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

    # Time gates
    TIME_REMAINING_TOO_LOW = "time_remaining_too_low"
    TIME_REMAINING_TOO_HIGH = "time_remaining_too_high"

    # Execution gates
    COOLDOWN = "cooldown"
    RISK_BLOCK = "risk_block"
    POSITION_CAP = "position_cap"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    KILL_SWITCH = "kill_switch"
    DAILY_LOSS_HALT = "daily_loss_halt"

    # Data quality gates
    STALE_ORDERBOOK = "stale_orderbook"
    EMPTY_ORDERBOOK = "empty_orderbook"
    FEED_WARMING = "feed_warming"
    FEED_STALE = "feed_stale"
    INVALID_BOOK = "invalid_book"
    NO_FEED = "no_feed"

    # Direction / consistency gates
    WRONG_DIRECTION = "wrong_direction"
    DIRECTION_CONFLICT = "direction_conflict"
    MOMENTUM_CONFLICT = "momentum_conflict"
    BURST_NOT_BTC_DRIVEN = "burst_not_btc_driven"

    # Misc
    AGGREGATOR_DROPPED = "aggregator_dropped"
    BTC_VOL_ZERO = "btc_vol_zero"
    BTC_FEED_INVALID = "btc_feed_invalid"


# Process-wide block-reason counter. Reset between cycles is intentional;
# this is meant to accumulate over the bot's lifetime so you can run
# `block_counter.most_common(10)` at any point.
block_counter: Counter[str] = Counter()


# ─────────────────────────────────────────────────────────────────────────────
#  File-based JSONL logger
# ─────────────────────────────────────────────────────────────────────────────


class _DecisionLogger:
    """Writes one JSON line per emit() call.

    Lazy file open; rotates daily based on UTC date. Thread-safe enough for
    asyncio (single writer per process). Uses os.fsync semantics on flush
    every N writes to bound data loss to ~N decisions on crash.
    """

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
            # Rotate yesterday's log if it exists
            base = Path(self._path)
            if base.exists() and self._current_date:
                rotated = base.with_name(f"{base.stem}.{self._current_date}{base.suffix}")
                # Only rotate if not already rotated today (e.g. on restart)
                if not rotated.exists():
                    try:
                        # We rename the existing file to yesterday's date if it
                        # has any content from before midnight UTC.
                        # Simple heuristic: rotate if last-modified is before UTC midnight today.
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
            # Logging must never break the trading loop.
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
    """Reconfigure the singleton decision logger (path, flush cadence)."""
    global _logger
    _logger.close()
    _logger = _DecisionLogger(
        path=path or _LOG_PATH,
        flush_every=flush_every,
    )


def shutdown() -> None:
    """Flush and close the decision log. Call from bot.stop()."""
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
    """Emit one decision-log line. Always called once per (strategy, market) per cycle."""
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
    }
    if extra:
        # Don't let extra fields clobber canonical ones
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
    """Return the most common block reasons. Call this from the dashboard."""
    return dict(block_counter.most_common(top_n))


def reset_counter() -> None:
    """Reset the block-reason counter. Use sparingly — most useful in tests."""
    block_counter.clear()
