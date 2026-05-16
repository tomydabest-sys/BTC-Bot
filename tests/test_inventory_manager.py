"""Tests for the maker inventory manager."""

from __future__ import annotations

import pytest

from polybot.positions.inventory import InventoryConfig, InventoryManager


@pytest.fixture
def inv():
    return InventoryManager(
        market_id="m-1",
        config=InventoryConfig(
            max_inventory_per_side=100.0,
            skew_cents_per_share=0.0005,
            max_skew_cents=0.05,
            delta_threshold_uncertain=0.005,
            delta_threshold_directional=0.15,
            directional_bet_price=0.95,
        ),
    )


def test_buy_yes_increments_long(inv):
    inv.on_fill(side_yes=True, is_buy=True, shares=10)
    assert inv.net_yes_shares == 10
    assert inv.is_long


def test_buy_no_decrements_yes(inv):
    inv.on_fill(side_yes=False, is_buy=True, shares=10)
    assert inv.net_yes_shares == -10
    assert inv.is_short


def test_sell_yes_decrements(inv):
    inv.on_fill(side_yes=True, is_buy=True, shares=20)
    inv.on_fill(side_yes=True, is_buy=False, shares=5)
    assert inv.net_yes_shares == 15


def test_inventory_cap_detection(inv):
    inv.on_fill(side_yes=True, is_buy=True, shares=100)
    assert inv.is_inventory_capped(side_yes=True, is_buy=True) is True
    # The opposite side is fine.
    assert inv.is_inventory_capped(side_yes=False, is_buy=True) is False


def test_skew_cents_caps_at_max(inv):
    inv.on_fill(side_yes=True, is_buy=True, shares=10_000)  # way over cap
    assert inv.skew_cents() == pytest.approx(0.05)


def test_skew_cents_signed_negative_for_short(inv):
    inv.on_fill(side_yes=False, is_buy=True, shares=50)
    skew = inv.skew_cents()
    assert skew < 0


def test_flatten_action_uncertain_holds_at_mid(inv):
    inv.on_fill(side_yes=True, is_buy=True, shares=10)
    decision = inv.flatten_action(fair_value=0.501, mid=0.50, time_remaining_s=8)
    assert decision.action == "noop"
    assert decision.reason == "uncertain_midprice"


def test_flatten_action_directional_yes(inv):
    inv.on_fill(side_yes=True, is_buy=True, shares=5)
    decision = inv.flatten_action(fair_value=0.80, mid=0.65, time_remaining_s=8)
    assert decision.action == "bet_yes"
    assert decision.target_price == pytest.approx(0.95)


def test_flatten_action_directional_no(inv):
    inv.on_fill(side_yes=True, is_buy=True, shares=5)
    decision = inv.flatten_action(fair_value=0.20, mid=0.30, time_remaining_s=8)
    assert decision.action == "bet_no"


def test_flatten_action_mid_zone_flattens(inv):
    inv.on_fill(side_yes=True, is_buy=True, shares=25)
    decision = inv.flatten_action(fair_value=0.58, mid=0.55, time_remaining_s=8)
    assert decision.action == "flatten"
    assert decision.size_shares == pytest.approx(25)


def test_reset_clears_inventory(inv):
    inv.on_fill(side_yes=True, is_buy=True, shares=42)
    inv.reset()
    assert inv.net_yes_shares == 0
