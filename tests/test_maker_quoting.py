"""Tests for the maker-quoting strategy: fair-spread math + staleness."""

from __future__ import annotations

import pytest

from polybot.strategies.fair_value import fee_at_price
from polybot.strategies.maker_quoting import (
    MakerQuotingConfig,
    MakerQuotingStrategy,
    NoQuote,
    Quote,
)


def test_half_spread_covers_round_trip_fee_at_midprice():
    strat = MakerQuotingStrategy(
        MakerQuotingConfig(min_half_spread_cents=0.0, adverse_selection_buffer_cents=0.0)
    )
    fair = 0.50
    fee = 0.072
    half = strat.compute_half_spread(
        fair_value=fair,
        fee_rate=fee,
        vol_annual=0.45,
        time_remaining_s=120.0,
    )
    # Round-trip fee at p=0.5, fee_rate=0.072 is 2 * 0.072 * 0.25 = 0.036.
    # Half-spread must cover at least half of it (0.018).
    assert half >= 0.018 - 1e-9


def test_half_spread_respects_floor():
    strat = MakerQuotingStrategy(
        MakerQuotingConfig(min_half_spread_cents=5.0, adverse_selection_buffer_cents=0.0)
    )
    # With a 5c floor, even an asymmetric fair value must clear 0.05.
    half = strat.compute_half_spread(
        fair_value=0.10,
        fee_rate=0.0,
        vol_annual=0.45,
        time_remaining_s=120.0,
    )
    assert half >= 0.05 - 1e-9


def test_half_spread_widens_with_vol():
    strat = MakerQuotingStrategy(
        MakerQuotingConfig(min_half_spread_cents=0.0, adverse_selection_buffer_cents=1.0)
    )
    low = strat.compute_half_spread(
        fair_value=0.5, fee_rate=0.072, vol_annual=0.20, time_remaining_s=120
    )
    high = strat.compute_half_spread(
        fair_value=0.5, fee_rate=0.072, vol_annual=1.20, time_remaining_s=120
    )
    assert high > low, "higher vol should widen the spread"


def test_half_spread_compresses_near_expiry():
    strat = MakerQuotingStrategy(
        MakerQuotingConfig(min_half_spread_cents=0.0, adverse_selection_buffer_cents=2.0)
    )
    early = strat.compute_half_spread(
        fair_value=0.5, fee_rate=0.072, vol_annual=0.45, time_remaining_s=120
    )
    late = strat.compute_half_spread(
        fair_value=0.5, fee_rate=0.072, vol_annual=0.45, time_remaining_s=2
    )
    assert late < early, "spread should compress as time runs out"


def test_compute_quotes_basic_pair():
    strat = MakerQuotingStrategy(MakerQuotingConfig(min_half_spread_cents=2.0))
    yes, no = strat.compute_quotes(
        fair_value=0.55,
        fee_rate=0.072,
        vol_annual=0.45,
        time_remaining_s=180,
        size_shares=10,
        inventory_skew_cents=0.0,
    )
    assert isinstance(yes, Quote) and isinstance(no, Quote)
    assert yes.side_yes is True and no.side_yes is False
    assert yes.is_buy is True and no.is_buy is True
    assert 0.01 <= yes.price <= 0.99
    assert 0.01 <= no.price <= 0.99
    # Quotes sit BELOW the side's fair value because the maker is buying.
    assert yes.price <= 0.55
    assert no.price <= 0.45


def test_compute_quotes_rejects_invalid_fair():
    strat = MakerQuotingStrategy(MakerQuotingConfig())
    with pytest.raises(NoQuote):
        strat.compute_quotes(
            fair_value=1.5, fee_rate=0.072, vol_annual=0.45,
            time_remaining_s=60, size_shares=5,
        )
    with pytest.raises(NoQuote):
        strat.compute_quotes(
            fair_value=0.5, fee_rate=0.072, vol_annual=0.45,
            time_remaining_s=60, size_shares=0,
        )


def test_inventory_skew_shifts_quotes_in_opposite_directions():
    strat = MakerQuotingStrategy(MakerQuotingConfig(min_half_spread_cents=2.0))
    base_yes, base_no = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=120, size_shares=5, inventory_skew_cents=0.0,
    )
    long_yes, long_no = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=120, size_shares=5, inventory_skew_cents=2.0,
    )
    # Skew positive (long YES) → YES bid moves down, NO bid moves up.
    assert long_yes.price <= base_yes.price
    assert long_no.price >= base_no.price


def test_is_quote_stale_keys_off_price_not_time():
    strat = MakerQuotingStrategy(MakerQuotingConfig())
    # 1c drift, threshold 1.5c → not stale
    assert strat.is_quote_stale(
        quote_reference_fair=0.50, current_fair=0.51, threshold_cents=1.5
    ) is False
    # 2c drift → stale
    assert strat.is_quote_stale(
        quote_reference_fair=0.50, current_fair=0.52, threshold_cents=1.5
    ) is True


def test_fee_at_price_matches_brief_math():
    # 2 * fee_at_price(0.5, 0.072) = 2 * 0.072 * 0.25 = 0.036 (3.6¢ round-trip)
    assert abs(2 * fee_at_price(0.5, 0.072) - 0.036) < 1e-9
    # Asymmetric prices: fee scales with p(1-p)
    assert fee_at_price(0.10, 0.072) < fee_at_price(0.50, 0.072)


# ─────────────────────────────────────────────────────────────────────────
# Inventory-aware one-sided quoting — stops catching the falling knife
# ─────────────────────────────────────────────────────────────────────────


def test_heavy_long_yes_suppresses_yes_bid_keeps_no():
    """Past the soft limit while long YES, the YES bid (which would add to
    the position) is dropped; the NO bid (which reduces it) keeps quoting."""
    strat = MakerQuotingStrategy(MakerQuotingConfig(inventory_soft_limit_ratio=0.5))
    yes_q, no_q = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=120, size_shares=5,
        net_inventory_shares=150, max_inventory_shares=200,  # 0.75 > 0.5 soft
    )
    assert yes_q.size == 0.0, "YES side must be suppressed when heavy long YES"
    assert no_q.size > 0.0, "NO side (reducing) must keep quoting"


def test_heavy_short_yes_suppresses_no_bid_keeps_yes():
    strat = MakerQuotingStrategy(MakerQuotingConfig(inventory_soft_limit_ratio=0.5))
    yes_q, no_q = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=120, size_shares=5,
        net_inventory_shares=-150, max_inventory_shares=200,
    )
    assert no_q.size == 0.0, "NO side must be suppressed when heavy short YES"
    assert yes_q.size > 0.0, "YES side (reducing) must keep quoting"


def test_within_soft_limit_quotes_both_sides():
    strat = MakerQuotingStrategy(MakerQuotingConfig(inventory_soft_limit_ratio=0.5))
    yes_q, no_q = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=120, size_shares=5,
        net_inventory_shares=50, max_inventory_shares=200,  # 0.25 < 0.5
    )
    assert yes_q.size > 0.0 and no_q.size > 0.0


def test_no_cap_means_no_suppression():
    """max_inventory_shares=0 (default) disables suppression entirely."""
    strat = MakerQuotingStrategy(MakerQuotingConfig())
    yes_q, no_q = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=120, size_shares=5,
        net_inventory_shares=10_000, max_inventory_shares=0,
    )
    assert yes_q.size > 0.0 and no_q.size > 0.0


# ─────────────────────────────────────────────────────────────────────────
# Vol-scaled trend filter — pulls the side the trend is running over
# ─────────────────────────────────────────────────────────────────────────


def test_downtrend_widens_then_suppresses_yes_bid():
    """Fair falling (-drift) picks off the YES bid. A small drift widens it
    (lower bid); a drift past half-spread pulls it entirely."""
    strat = MakerQuotingStrategy(MakerQuotingConfig(
        min_half_spread_cents=2.0, adverse_selection_buffer_cents=0.0,
        trend_suppress_ratio=1.0,
    ))
    base_yes, base_no = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.0, vol_annual=0.45,
        time_remaining_s=120, size_shares=5, trend_drift=0.0,
    )
    # Small downward drift (1c) < half-spread (2c) → YES bid widened lower.
    small_yes, small_no = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.0, vol_annual=0.45,
        time_remaining_s=120, size_shares=5, trend_drift=-0.01,
    )
    assert small_yes.price < base_yes.price, "down-drift must lower the YES bid"
    assert small_no.price == base_no.price, "NO side unaffected by down-drift"

    # Strong downward drift (3c) >= half-spread (2c) → YES bid suppressed.
    strong_yes, strong_no = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.0, vol_annual=0.45,
        time_remaining_s=120, size_shares=5, trend_drift=-0.03,
    )
    assert strong_yes.size == 0.0, "strong down-trend must pull the YES bid"
    assert strong_no.size > 0.0, "NO side keeps quoting in a down-trend"


def test_uptrend_suppresses_no_bid():
    strat = MakerQuotingStrategy(MakerQuotingConfig(
        min_half_spread_cents=2.0, adverse_selection_buffer_cents=0.0,
        trend_suppress_ratio=1.0,
    ))
    yes_q, no_q = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.0, vol_annual=0.45,
        time_remaining_s=120, size_shares=5, trend_drift=0.03,
    )
    assert no_q.size == 0.0, "strong up-trend must pull the NO bid"
    assert yes_q.size > 0.0, "YES side keeps quoting in an up-trend"


def test_trend_trip_point_scales_with_half_spread():
    """The suppression trip point is trend_suppress_ratio x half-spread, and
    half-spread grows with vol — so the same drift is tolerated in high vol
    but pulls the quote in calm vol (the vol-scaling property)."""
    # adverse_selection_buffer makes half-spread vol-sensitive.
    strat = MakerQuotingStrategy(MakerQuotingConfig(
        min_half_spread_cents=1.0, adverse_selection_buffer_cents=4.0,
        reference_annual_vol=0.45, trend_suppress_ratio=1.0,
    ))
    drift = -0.04
    # Calm vol → small half-spread → drift trips suppression.
    calm_yes, _ = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.0, vol_annual=0.10,
        time_remaining_s=120, size_shares=5, trend_drift=drift,
    )
    # High vol → wider half-spread → same drift tolerated (still quotes).
    wild_yes, _ = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.0, vol_annual=1.10,
        time_remaining_s=120, size_shares=5, trend_drift=drift,
    )
    assert calm_yes.size == 0.0, "calm vol: tight spread → drift pulls YES bid"
    assert wild_yes.size > 0.0, "high vol: wide spread absorbs the same drift"


def test_no_trend_drift_leaves_quotes_symmetric():
    strat = MakerQuotingStrategy(MakerQuotingConfig())
    yes_q, no_q = strat.compute_quotes(
        fair_value=0.50, fee_rate=0.072, vol_annual=0.45,
        time_remaining_s=120, size_shares=5, trend_drift=0.0,
    )
    assert yes_q.size > 0.0 and no_q.size > 0.0
