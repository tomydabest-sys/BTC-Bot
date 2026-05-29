"""Entry-price band performance report (the brief's go/no-go lens)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from polybot.polyweather.analysis.band_report import (
    band_for,
    band_report,
    format_report,
)


@dataclass
class _T:
    entry_price: Decimal
    realised_pnl_usdc: Decimal
    strategy: str = "weather_ensemble"
    city: str = "London"
    realised_outcome: int | None = None


def test_band_for_boundaries() -> None:
    assert band_for(0.04) == "<5c"
    assert band_for(0.05) == "5-15c"
    assert band_for(0.15) == "15-35c"
    assert band_for(0.84) == "65-85c"
    assert band_for(0.85) == "85-95c"
    assert band_for(0.96) == ">=95c"


def test_band_report_aggregates_pnl_and_winrate() -> None:
    trades = [
        _T(Decimal("0.10"), Decimal("-5.00")),   # 5-15c loser
        _T(Decimal("0.12"), Decimal("-3.00")),   # 5-15c loser
        _T(Decimal("0.40"), Decimal("8.00")),    # 35-50c winner
        _T(Decimal("0.45"), Decimal("6.00")),    # 35-50c winner
        _T(Decimal("0.70"), Decimal("-2.00")),   # 65-85c loser
    ]
    rep = band_report(trades)

    assert rep.total_trades == 5
    assert rep.total_pnl == Decimal("4.0000")

    tail = rep.by_band["5-15c"]
    assert tail.markets == 2 and tail.wins == 0
    assert tail.total_pnl == Decimal("-8.00")

    mid = rep.by_band["35-50c"]
    assert mid.markets == 2 and mid.wins == 2
    assert mid.win_rate == 1.0
    assert mid.pnl_per_market == Decimal("7.0000")

    # 3 of 5 trades fall in the 15-85c gate band.
    assert rep.in_middle_band == 3
    assert abs(rep.in_band_share - 0.6) < 1e-9


def test_realised_outcome_drives_win_when_present() -> None:
    # A trade can be flat/negative P&L but recorded as a YES win, or vice versa.
    trades = [
        _T(Decimal("0.40"), Decimal("0.00"), realised_outcome=1),
        _T(Decimal("0.40"), Decimal("0.00"), realised_outcome=0),
    ]
    rep = band_report(trades)
    assert rep.by_band["35-50c"].wins == 1


def test_by_strategy_and_city_totals() -> None:
    trades = [
        _T(Decimal("0.40"), Decimal("5.00"), strategy="weather_ensemble", city="London"),
        _T(Decimal("0.40"), Decimal("-2.00"), strategy="resolution_meanrev", city="Paris"),
    ]
    rep = band_report(trades)
    assert rep.by_strategy["weather_ensemble"] == Decimal("5.00")
    assert rep.by_strategy["resolution_meanrev"] == Decimal("-2.00")
    assert rep.by_city["London"] == Decimal("5.00")
    assert rep.by_city["Paris"] == Decimal("-2.00")


def test_empty_report_is_safe() -> None:
    rep = band_report([])
    assert rep.total_trades == 0
    assert "No closed trades yet" in format_report(rep)


def test_format_report_flags_tail_and_bands() -> None:
    trades = [
        _T(Decimal("0.02"), Decimal("-1.00")),   # <5c bleed
        _T(Decimal("0.45"), Decimal("9.00")),    # winner
    ]
    out = format_report(band_report(trades))
    assert "sub-15c long-shot" in out
    assert "net positive bands" in out
