"""Kelly position sizing with confidence scaling and per-timeframe caps.

PATCHED FROM ORIGINAL:
- min_usd: 5.0 → 2.0  (critical fix: at $500 bankroll Q-Kelly often produces
                       $1.25–$3.10 sizes that were blocked by the $5 floor)
- Added size_calc debug log inside position_size for diagnostic clarity

Replaces flat `bankroll * size_pct` with a fee-aware fractional Kelly that
shrinks with timeframe noise and caps at a hard percentage of bankroll.

The formula:
    f_kelly = max(0, (b * p_win - q) / b)         # Kelly fraction
    f       = kelly_fraction * f_kelly * confidence
    f       = min(f, timeframe_cap, hard_cap_pct)
    size    = f * bankroll
"""

from __future__ import annotations

import math
import structlog
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Sequence

logger = structlog.get_logger()


@dataclass
class SizingResult:
    """Output of a sizing decision, with full rationale for the decision log."""
    size_usd: float
    kelly_f: float
    capped_by: str  # "timeframe" / "hard_cap" / "min_usd" / "edge_floor" / "kelly" / "ok"
    notes: str = ""

    def __bool__(self) -> bool:
        return self.size_usd > 0


@dataclass
class SizingPolicy:
    """Configuration for Kelly sizing. Loaded from RiskConfig."""
    bankroll_usd: float = 500.0
    kelly_fraction: float = 0.50          # PATCHED: was 0.25 (Quarter-Kelly)
    hard_cap_pct: float = 0.10            # PATCHED: was 0.06
    edge_floor_bps: float = 3.0           # PATCHED: was 10.0
    min_usd: float = 2.0                  # PATCHED: was 5.0 (CRITICAL FIX)
    per_timeframe_cap_pct: dict[str, float] = field(default_factory=lambda: {
        "5m": 0.04,                       # PATCHED: was 0.02
        "15m": 0.05,                      # PATCHED: was 0.03
        "1h": 0.05,
        "4h": 0.06,
        "daily": 0.06,
    })


def position_size(
    *,
    bankroll: float,
    p_win: float,
    avg_win: float,
    avg_loss: float,
    edge_bps: float,
    confidence: float,
    timeframe: str = "",
    policy: SizingPolicy | None = None,
) -> SizingResult:
    """Compute Half-Kelly position size with confidence and timeframe caps."""
    pol = policy or SizingPolicy()

    # ── Edge floor ──────────────────────────────────────────────────
    if edge_bps < pol.edge_floor_bps:
        result = SizingResult(0.0, 0.0, "edge_floor",
                              f"edge {edge_bps:.1f}bps < floor {pol.edge_floor_bps:.1f}bps")
        logger.debug("size_calc", p_win=round(p_win, 4), edge_bps=round(edge_bps, 2),
                     conf=round(confidence, 3), tf=timeframe, result_usd=0.0,
                     capped_by="edge_floor")
        return result

    # ── Kelly degenerate cases ──────────────────────────────────────
    if avg_loss <= 0 or p_win <= 0 or p_win >= 1:
        return SizingResult(0.0, 0.0, "kelly", "degenerate p_win or avg_loss")

    b = avg_win / avg_loss
    q = 1.0 - p_win
    f_kelly_raw = (b * p_win - q) / b if b > 0 else 0.0
    f_kelly = max(0.0, f_kelly_raw)

    if f_kelly <= 0:
        result = SizingResult(0.0, 0.0, "kelly",
                              f"Kelly negative (b={b:.3f}, p={p_win:.3f})")
        logger.debug("size_calc", p_win=round(p_win, 4), edge_bps=round(edge_bps, 2),
                     conf=round(confidence, 3), tf=timeframe, result_usd=0.0,
                     capped_by="kelly_negative", b=round(b, 3))
        return result

    # ── Half-Kelly × confidence ─────────────────────────────────────
    f = pol.kelly_fraction * f_kelly * max(0.0, min(1.0, confidence))

    # ── Per-timeframe cap ───────────────────────────────────────────
    tf_cap = pol.per_timeframe_cap_pct.get(timeframe)
    capped_by = "ok"
    if tf_cap is not None and f > tf_cap:
        f = tf_cap
        capped_by = "timeframe"

    # ── Hard cap ────────────────────────────────────────────────────
    if f > pol.hard_cap_pct:
        f = pol.hard_cap_pct
        capped_by = "hard_cap"

    size = f * bankroll

    if size < pol.min_usd:
        result = SizingResult(0.0, f, "min_usd",
                              f"size ${size:.2f} < min ${pol.min_usd:.2f}")
        logger.debug("size_calc", p_win=round(p_win, 4), edge_bps=round(edge_bps, 2),
                     conf=round(confidence, 3), tf=timeframe,
                     f_kelly=round(f_kelly, 4), f=round(f, 4),
                     result_usd=round(size, 2), capped_by="min_usd_floor",
                     min_usd=pol.min_usd)
        return result

    logger.debug("size_calc", p_win=round(p_win, 4), edge_bps=round(edge_bps, 2),
                 conf=round(confidence, 3), tf=timeframe,
                 f_kelly=round(f_kelly, 4), f=round(f, 4),
                 result_usd=round(size, 2), capped_by=capped_by)

    return SizingResult(size_usd=size, kelly_f=f, capped_by=capped_by,
                        notes=f"f_kelly_raw={f_kelly_raw:.4f} b={b:.3f}")


def derive_p_win_from_signal(
    target_price: float,
    fair_value: float,
    direction_buy: bool,
) -> tuple[float, float, float]:
    """Derive (p_win, avg_win, avg_loss) from binary-market geometry."""
    p = max(0.001, min(0.999, target_price))
    f = max(0.001, min(0.999, fair_value))
    if direction_buy:
        return f, (1.0 - p), p
    else:
        return (1.0 - f), p, (1.0 - p)


def expected_value_per_dollar(
    p_win: float, avg_win: float, avg_loss: float,
) -> float:
    """E[$ return per $1 stake] = p*avg_win - q*avg_loss."""
    return p_win * avg_win - (1.0 - p_win) * avg_loss
