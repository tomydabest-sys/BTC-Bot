"""Kelly position sizing with confidence scaling and per-timeframe caps.

Replaces flat `bankroll * size_pct` with a fee-aware fractional Kelly that
shrinks with timeframe noise and caps at a hard percentage of bankroll.

The formula:
    f_kelly = max(0, (b * p_win - q) / b)         # Kelly fraction
    f       = kelly_fraction * f_kelly * confidence
    f       = min(f, timeframe_cap, hard_cap_pct)
    size    = f * bankroll

Where:
    b       = avg_win / avg_loss
    p_win   = model probability of win (calibrated where possible)
    q       = 1 - p_win

Default kelly_fraction=0.25 (Quarter-Kelly) reflects retail-crypto consensus
that Half-Kelly maximises geometric growth in well-calibrated markets but
Quarter-Kelly survives miscalibration without ruin.

Per-timeframe caps prevent any single 5m signal from disproportionately
sizing relative to a 4h or daily signal — shorter timeframes are noisier.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


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
    kelly_fraction: float = 0.25
    hard_cap_pct: float = 0.06
    edge_floor_bps: float = 10.0
    min_usd: float = 5.0
    per_timeframe_cap_pct: dict[str, float] = field(default_factory=lambda: {
        "5m": 0.02,
        "15m": 0.03,
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
    """Compute Quarter-Kelly position size with confidence and timeframe caps.

    Args:
        bankroll: Current bankroll in USD (use config.risk.bankroll_usd).
        p_win: Probability of winning (calibrated where possible). Range [0, 1].
        avg_win: Expected win amount per $1 stake. For binary at price p,
            avg_win ≈ (1 - p) / p × p = 1 - p.
        avg_loss: Expected loss per $1 stake. For binary at price p, avg_loss ≈ p.
        edge_bps: Net (post-fee) edge in basis points.
        confidence: Strategy-reported confidence in [0, 1].
        timeframe: Canonical code ("5m", "15m", "1h", "4h", "daily").
        policy: SizingPolicy or None (uses defaults).

    Returns:
        SizingResult with the dollar size and a `capped_by` rationale.
    """
    pol = policy or SizingPolicy()

    # ── Edge floor ──────────────────────────────────────────────────
    if edge_bps < pol.edge_floor_bps:
        return SizingResult(0.0, 0.0, "edge_floor",
                            f"edge {edge_bps:.1f}bps < floor {pol.edge_floor_bps:.1f}bps")

    # ── Kelly degenerate cases ──────────────────────────────────────
    if avg_loss <= 0 or p_win <= 0 or p_win >= 1:
        return SizingResult(0.0, 0.0, "kelly", "degenerate p_win or avg_loss")

    b = avg_win / avg_loss
    q = 1.0 - p_win
    f_kelly_raw = (b * p_win - q) / b if b > 0 else 0.0
    f_kelly = max(0.0, f_kelly_raw)

    if f_kelly <= 0:
        return SizingResult(0.0, 0.0, "kelly",
                            f"Kelly negative (b={b:.3f}, p={p_win:.3f})")

    # ── Quarter-Kelly × confidence ──────────────────────────────────
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
        return SizingResult(0.0, f, "min_usd",
                            f"size ${size:.2f} < min ${pol.min_usd:.2f}")

    return SizingResult(size_usd=size, kelly_f=f, capped_by=capped_by,
                        notes=f"f_kelly_raw={f_kelly_raw:.4f} b={b:.3f}")


def derive_p_win_from_signal(
    target_price: float,
    fair_value: float,
    direction_buy: bool,
) -> tuple[float, float, float]:
    """Derive (p_win, avg_win, avg_loss) from binary-market geometry.

    For BUY at price p with model fair value f:
        p_win   = f                 (probability we collect $1)
        avg_win = 1 - p             (gain per $1 stake on win)
        avg_loss= p                 (loss per $1 stake on loss)

    For SELL (= BUY the opposite outcome at 1-p):
        Flip the perspective:
        p_win   = 1 - f
        avg_win = p
        avg_loss= 1 - p

    Returns:
        (p_win, avg_win, avg_loss)
    """
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
