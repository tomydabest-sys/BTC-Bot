"""Structured per-cycle decision log + block-reason counter.

PATCHED v2:
1. Daily rotation now correctly handles multi-day rollovers — the previous
   logic would silently fail on the second day rollover because it tried to
   rename an already-renamed file.
2. Added new BlockReason codes for diagnosing zero-trade scenarios
3. Added FORCE_TRADE flag and relax() helper for force-trade mode
4. Added stage-numbered overshoot block reasons (OVR_01..OVR_08)
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
#  FORCE-TRADE MODE
# ─────────────────────────────────────────────────────────────────────────────

FORCE_TRADE = os.environ.get("BOT_FORCE_TRADE") == "1"


def relax(value: float, factor: float = 0.5, floor: float | None = None) -> float:
    """Halve gates when in force-trade mode."""
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

    # Data freshness diagnostics
    POLY_FEED_NEVER_ARRIVED = "poly_feed_never_arrived"
    BTC_FEED_NOT_SUBSCRIBED = "btc_feed_not_subscribed"
    ORDERBOOK_TOO_LATE = "orderbook_too_late"
    MARKET_TOO_NEW = "market_too_new"
    MID_NOT_MOVING = "mid_not_moving"

    # Sizing diagnostics
    SIZED_TO_ZERO = "sized_to_zero"
    KELLY_NEGATIVE = "kelly_negative"

    # Aggregator-specific
    AGG_DROPPED_LOW_CONF = "agg_dropped_low_conf"
    AGG_CONFLICT_ABSTAIN = "agg_conflict_abstain"

    # Stage-numbered overshoot lifecycle
    OVR_01_FEED_WARMING = "ovr_01_feed_warming"
    OVR_02_TIME_RANGE = "ovr_02_time_range"
    OVR_03_BOOK_INVALID = "ovr_03_book_invalid"
    OVR_04_BTC_MOVE = "ovr_04_btc_move"
    OVR_05_NO_BURST = "ovr_05_no_burst"
    OVR_06_DIRECTION = "ovr_06_direction"
    OVR_07_OVERSHOOT_RANGE = "ovr_07_overshoot_range"
    OVR_08_EDGE = "ovr_08_edge"

    # Maker-quoting lifecycle (V2)
    SPREAD_UNECONOMIC = "spread_uneconomic"
    INVENTORY_LIMIT = "inventory_limit"
    QUOTE_STALE = "quote_stale"
    FLATTEN_TRIGGERED = "flatten_triggered"
    CANCEL_REPLACE_SLOW = "cancel_replace_slow"
    FEED_DISCONNECT = "feed_disconnect"
    FEE_RATE_CHANGED = "fee_rate_changed"
    BATCH_LIMIT_EXCEEDED = "batch_limit_exceeded"
    SDK_NOT_INSTALLED = "sdk_not_installed"


# Process-wide block-reason counter
block_counter: Counter[str] = Counter()


# ─────────────────────────────────────────────────────────────────────────────
#  File-based JSONL logger with daily rotation
# ─────────────────────────────────────────────────────────────────────────────


class _DecisionLogger:
    """Writes one JSON line per emit() call with daily rotation.

    Rotation policy:
    - File path is fixed (e.g. logs/decisions.jsonl)
    - On day boundary, current file is closed and (atomically) renamed to
      logs/decisions.YYYY-MM-DD.jsonl using the date the file was last written.
    - A fresh decisions.jsonl is opened for today's entries.
    - Subsequent days repeat the rotation cleanly.
    """

    def __init__(
        self,
        path: str = "logs/decisions.jsonl",
        flush_every: int = 25,
    ) -> None:
        self._path = Path(path)
        self._flush_every = flush_every
        self._fh = None
        self._writes_since_flush = 0
        # Date the currently-open file is associated with (UTC date string)
        self._open_date: str | None = None
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _today_date_str(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _file_mtime_date_str(self) -> str | None:
        """Return the UTC date of the existing file's last modification, or None."""
        try:
            mtime = self._path.stat().st_mtime
            return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def _rotate_if_needed(self) -> None:
        """Close + rename if the open file belongs to a previous day."""
        today = self._today_date_str()

        # First call: figure out what day the existing file (if any) is from
        if self._open_date is None:
            file_date = self._file_mtime_date_str()
            if file_date is not None and file_date != today:
                # Close handle if open, then rename the stale file
                self._close_handle()
                self._archive_file(file_date)
            # After this block, _open_date will be set to today by _open_handle
            return

        if self._open_date != today:
            # Day rolled over while we were running; rotate the file
            self._close_handle()
            self._archive_file(self._open_date)

    def _archive_file(self, date_str: str) -> None:
        """Rename the current path to its dated archive name. Best-effort."""
        if not self._path.exists():
            return
        archive = self._path.with_name(f"{self._path.stem}.{date_str}{self._path.suffix}")
        # If archive already exists (rare — manual run, restart, etc), append a counter
        if archive.exists():
            n = 1
            while True:
                alt = self._path.with_name(
                    f"{self._path.stem}.{date_str}.{n}{self._path.suffix}"
                )
                if not alt.exists():
                    archive = alt
                    break
                n += 1
                if n > 1000:
                    return  # give up rather than spin
        try:
            self._path.rename(archive)
        except OSError:
            # Windows: another process may hold a handle; skip rotation this cycle
            pass

    def _close_handle(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def _open_handle(self):
        self._rotate_if_needed()
        if self._fh is None:
            self._fh = open(self._path, "a", encoding="utf-8", buffering=1)
            self._open_date = self._today_date_str()
        return self._fh

    def write(self, payload: dict) -> None:
        try:
            fh = self._open_handle()
            fh.write(json.dumps(payload, default=_json_default) + "\n")
            self._writes_since_flush += 1
            if self._writes_since_flush >= self._flush_every:
                fh.flush()
                self._writes_since_flush = 0
        except Exception:
            pass

    def close(self) -> None:
        self._close_handle()


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
