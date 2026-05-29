"""Entry-price band performance report.

Operationalises the weather-wallet brief's go/no-go check (§5.3.5 / §6.5):
*does the bot make money in the uncertain middle (~15-85c) and avoid bleeding
in the cheap long-shot tail?* This reads the bot's own paper-trade history and
aggregates win-rate and P&L by the entry-price bands the brief used, so the
operator can validate the middle-band strategy during the 14-day paper run.

Read-only: it touches no trading logic. ``band_report`` is a pure function over
any objects exposing ``entry_price`` and ``realised_pnl_usdc`` (Decimal), plus
optional ``strategy`` / ``city`` / ``realised_outcome``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

# The brief's bands (lower inclusive, upper exclusive), in probability units.
BANDS: tuple[tuple[float, float, str], ...] = (
    (0.00, 0.05, "<5c"),
    (0.05, 0.15, "5-15c"),
    (0.15, 0.35, "15-35c"),
    (0.35, 0.50, "35-50c"),
    (0.50, 0.65, "50-65c"),
    (0.65, 0.85, "65-85c"),
    (0.85, 0.95, "85-95c"),
    (0.95, 1.01, ">=95c"),
)

# The strategy gate trades inside this band by default (strategy_weights.yaml).
MIDDLE_BAND = (0.15, 0.85)


def band_for(price: float) -> str:
    for lo, hi, label in BANDS:
        if lo <= price < hi:
            return label
    return ">=95c" if price >= 0.95 else "<5c"


@dataclass
class BandStat:
    band: str
    markets: int = 0
    wins: int = 0
    total_pnl: Decimal = Decimal("0")

    @property
    def win_rate(self) -> float:
        return self.wins / self.markets if self.markets else 0.0

    @property
    def pnl_per_market(self) -> Decimal:
        if not self.markets:
            return Decimal("0")
        return (self.total_pnl / self.markets).quantize(Decimal("0.0001"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "band": self.band,
            "markets": self.markets,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": self.total_pnl,
            "pnl_per_market": self.pnl_per_market,
        }


@dataclass
class BandReport:
    by_band: dict[str, BandStat] = field(default_factory=dict)
    by_strategy: dict[str, Decimal] = field(default_factory=dict)
    by_city: dict[str, Decimal] = field(default_factory=dict)
    total_trades: int = 0
    total_pnl: Decimal = Decimal("0")
    in_middle_band: int = 0  # trades with entry in MIDDLE_BAND

    @property
    def in_band_share(self) -> float:
        return self.in_middle_band / self.total_trades if self.total_trades else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "total_pnl": self.total_pnl,
            "in_band_share": round(self.in_band_share, 4),
            "by_band": [self.by_band[lbl].as_dict() for _, _, lbl in BANDS if lbl in self.by_band],
            "by_strategy": dict(self.by_strategy),
            "by_city": dict(self.by_city),
        }


def _is_win(trade: Any, pnl: Decimal) -> bool:
    # Prefer the recorded binary outcome where present; otherwise positive P&L.
    outcome = getattr(trade, "realised_outcome", None)
    if outcome in (0, 1):
        return bool(outcome)
    return pnl > 0


def band_report(trades: Iterable[Any]) -> BandReport:
    rep = BandReport()
    for t in trades:
        price = float(t.entry_price)
        pnl = Decimal(str(t.realised_pnl_usdc))
        label = band_for(price)
        stat = rep.by_band.setdefault(label, BandStat(band=label))
        stat.markets += 1
        stat.total_pnl += pnl
        if _is_win(t, pnl):
            stat.wins += 1

        rep.total_trades += 1
        rep.total_pnl += pnl
        if MIDDLE_BAND[0] <= price <= MIDDLE_BAND[1]:
            rep.in_middle_band += 1

        strat = getattr(t, "strategy", "") or "?"
        city = getattr(t, "city", "") or "?"
        rep.by_strategy[strat] = rep.by_strategy.get(strat, Decimal("0")) + pnl
        rep.by_city[city] = rep.by_city.get(city, Decimal("0")) + pnl

    rep.total_pnl = rep.total_pnl.quantize(Decimal("0.0001"))
    return rep


def format_report(rep: BandReport) -> str:
    lines: list[str] = []
    lines.append("Entry-price band performance (the brief's go/no-go lens)")
    lines.append("=" * 62)
    if rep.total_trades == 0:
        lines.append("No closed trades yet — run the bot in paper, then re-run.")
        return "\n".join(lines)
    lines.append(f"{'band':>8} {'mkts':>6} {'win%':>7} {'total $':>12} {'$/mkt':>10}")
    for _lo, _hi, label in BANDS:
        s = rep.by_band.get(label)
        if not s:
            continue
        lines.append(
            f"{label:>8} {s.markets:>6} {s.win_rate * 100:>6.1f}% "
            f"{float(s.total_pnl):>12.2f} {float(s.pnl_per_market):>10.2f}"
        )
    lines.append("-" * 62)
    lines.append(
        f"total trades {rep.total_trades}  total P&L ${float(rep.total_pnl):.2f}  "
        f"in 15-85c band {rep.in_band_share * 100:.1f}%"
    )
    # Verdict: where is the money made vs lost?
    winners = [lbl for _, _, lbl in BANDS
               if (s := rep.by_band.get(lbl)) and s.total_pnl > 0]
    losers = [lbl for _, _, lbl in BANDS
              if (s := rep.by_band.get(lbl)) and s.total_pnl < 0]
    if winners:
        lines.append("  + net positive bands: " + ", ".join(winners))
    if losers:
        lines.append("  - net negative bands: " + ", ".join(losers))
    tail = rep.by_band.get("<5c"), rep.by_band.get("5-15c")
    tail_mkts = sum(s.markets for s in tail if s)
    if tail_mkts:
        tail_pnl = sum((s.total_pnl for s in tail if s), start=Decimal("0"))
        lines.append(
            f"  ! {tail_mkts} sub-15c long-shot trades (the bleed band), "
            f"net ${float(tail_pnl):.2f} — these should be rare under the band gate"
        )
    return "\n".join(lines)
