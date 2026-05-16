"""Inventory tracker for maker quoting.

The maker bot accumulates signed YES exposure across a single market: a
YES BUY adds shares; a NO BUY *also* adds shares to the opposite side
(short YES). We track net YES exposure in shares.

Key behaviours:
  * skew_cents(): how far to offset the next quote pair to lean against
    inventory, capped at `max_skew_cents`.
  * is_inventory_capped(): hard short-circuit when a side has hit its cap.
  * flatten_action(): at T-10s before expiry, decide whether to flatten
    or convert to a directional bet.

The class is intentionally pure-logic: it does not place orders. The
QuoteManager (or main loop) consumes its recommendations.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class InventoryConfig:
    """All cents are price-points (0.01 = 1¢)."""

    max_inventory_per_side: float = 200.0
    skew_cents_per_share: float = 0.0005  # 0.05¢ per share
    max_skew_cents: float = 0.05  # ±5¢ cap
    delta_threshold_uncertain: float = 0.005
    delta_threshold_directional: float = 0.15
    directional_bet_price: float = 0.95


@dataclass
class FlattenDecision:
    """Returned by `flatten_action()` near expiry."""

    action: str  # "noop" | "flatten" | "bet_yes" | "bet_no"
    size_shares: float = 0.0
    target_price: float = 0.0
    reason: str = ""


class InventoryManager:
    """Single-market inventory tracker. Construct one per active market."""

    def __init__(
        self,
        market_id: str,
        config: InventoryConfig | None = None,
    ) -> None:
        self._market_id = market_id
        self._config = config or InventoryConfig()
        self._net_yes_shares: float = 0.0

    # ─────────────────────────────────────────────────────────────────
    #  Mutation
    # ─────────────────────────────────────────────────────────────────

    def on_fill(self, *, side_yes: bool, is_buy: bool, shares: float) -> None:
        """Update net inventory after a fill.

        side_yes=True + is_buy=True  → +shares  (long YES)
        side_yes=True + is_buy=False → -shares  (short YES via sell)
        side_yes=False + is_buy=True → -shares  (long NO = short YES)
        side_yes=False + is_buy=False→ +shares  (sell NO = long YES)
        """
        if shares <= 0:
            return
        signed = shares if (side_yes == is_buy) else -shares
        self._net_yes_shares += signed

    def reset(self) -> None:
        self._net_yes_shares = 0.0

    # ─────────────────────────────────────────────────────────────────
    #  Read-only accessors
    # ─────────────────────────────────────────────────────────────────

    @property
    def market_id(self) -> str:
        return self._market_id

    @property
    def net_yes_shares(self) -> float:
        return self._net_yes_shares

    @property
    def is_long(self) -> bool:
        return self._net_yes_shares > 0

    @property
    def is_short(self) -> bool:
        return self._net_yes_shares < 0

    def is_inventory_capped(self, *, side_yes: bool, is_buy: bool) -> bool:
        """Would another fill on this side breach the cap?"""
        cap = self._config.max_inventory_per_side
        projected = self._net_yes_shares + (1.0 if (side_yes == is_buy) else -1.0)
        if projected > cap:
            return True
        if projected < -cap:
            return True
        return False

    # ─────────────────────────────────────────────────────────────────
    #  Skew and flatten logic
    # ─────────────────────────────────────────────────────────────────

    def skew_cents(self) -> float:
        """Half-spread offset to discourage further fills on the heavy side.

        Positive return = shift quotes DOWN (lean against long position).
        Negative return = shift quotes UP   (lean against short position).
        """
        raw = self._net_yes_shares * self._config.skew_cents_per_share
        cap = self._config.max_skew_cents
        if raw > cap:
            return cap
        if raw < -cap:
            return -cap
        return raw

    def flatten_action(
        self,
        *,
        fair_value: float,
        mid: float,
        time_remaining_s: float,
    ) -> FlattenDecision:
        """Decide what to do at T-flatten.

        delta = fair_value - 0.5 (signed certainty toward YES).

        |delta| < uncertain    → stay at midprice; accept the coin flip.
        |delta| >= directional → bet on the strong side at ~95c.
        Otherwise              → flatten by quoting through the book.
        """
        delta = fair_value - 0.5
        abs_d = abs(delta)
        net = self._net_yes_shares

        if abs_d >= self._config.delta_threshold_directional:
            # Strong signal: bet the likely side at near-certainty.
            if delta > 0:
                return FlattenDecision(
                    action="bet_yes",
                    size_shares=max(0.0, self._config.max_inventory_per_side - net),
                    target_price=self._config.directional_bet_price,
                    reason="directional_yes",
                )
            return FlattenDecision(
                action="bet_no",
                size_shares=max(0.0, self._config.max_inventory_per_side + net),
                target_price=self._config.directional_bet_price,
                reason="directional_no",
            )

        if abs_d < self._config.delta_threshold_uncertain:
            return FlattenDecision(
                action="noop",
                size_shares=0.0,
                target_price=mid,
                reason="uncertain_midprice",
            )

        if abs(net) < 1.0:
            return FlattenDecision(
                action="noop",
                size_shares=0.0,
                target_price=mid,
                reason="already_flat",
            )

        return FlattenDecision(
            action="flatten",
            size_shares=abs(net),
            target_price=mid,
            reason="flatten_to_mid",
        )
