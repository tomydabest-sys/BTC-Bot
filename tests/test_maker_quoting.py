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
