"""Two-sided maker quoting strategy — V2 primary income source.

The taker-side ``maker_edge`` strategy approximates this by posting limit
orders one tick inside the spread. ``MakerQuotingStrategy`` is the full
maker primitive the V2 architecture needs: it computes a fee-aware
spread off the Black–Scholes fair value, then returns a pair of
(YES_bid, NO_bid) quotes that the QuoteManager will sync to the book.

Critical fee math (per the brief):
    fee_at_price(p, fee_rate) = fee_rate * p * (1 - p)
    round_trip_fee = 2 * fee_at_price(p, fee_rate)

The minimum profitable half-spread is::

    min_half_spread = (round_trip_fee + adverse_sel_buffer) / 2

If the configured floor is tighter than this, we widen — never quote
into a guaranteed loss.

The strategy is intentionally stateless except for its config; live
inventory skew is supplied by `InventoryManager.skew_cents()` at
quote-time so the same compute helper is reusable from offline tools
(unit tests, paper-mode replay).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from polybot.strategies.fair_value import fee_at_price


@dataclass
class MakerQuotingConfig:
    min_half_spread_cents: float = 2.0
    adverse_selection_buffer_cents: float = 1.0
    # When time runs out, adverse selection has less time to bite, so we
    # let the spread compress toward the floor. Above this time we use
    # the full vol-scaled buffer.
    time_decay_horizon_s: float = 60.0
    # Wider on higher-vol regimes. Multiplied by ann.vol / reference vol.
    reference_annual_vol: float = 0.45
    vol_scaling_max: float = 2.5
    # Inventory soft-limit: once |net inventory| exceeds this fraction of the
    # per-side cap, stop quoting the side that would ADD to the position and
    # only quote the reducing side. Prevents one-directional accumulation
    # (catching a falling knife) and the hard-cap stall where both sides
    # freeze and the position is held to expiry.
    inventory_soft_limit_ratio: float = 0.5
    # Trend filter: when fair value is drifting directionally, the bid on the
    # side losing value gets picked off on every tick. We widen that exposed
    # side's bid by the projected adverse drift, and suppress it entirely once
    # that drift reaches `trend_suppress_ratio` x half-spread (the spread can
    # no longer cover the move). The half-spread is vol-scaled, so the trip
    # point auto-loosens in high-vol regimes — the "vol-scaled" trend filter.
    trend_suppress_ratio: float = 1.0


@dataclass
class Quote:
    side_yes: bool        # True = YES side, False = NO side
    is_buy: bool          # True for bids (always True for maker bids)
    price: float          # in [0.01, 0.99]
    size: float           # shares
    reference_fair: float
    reference_mid: float


class NoQuoteError(Exception):
    """Raised when no economically viable quote exists for a side."""


# Backwards-compatible alias.
NoQuote = NoQuoteError


class MakerQuotingStrategy:
    """Pure-function quote computation.

    Use:
        strat = MakerQuotingStrategy(MakerQuotingConfig())
        yes_quote, no_quote = strat.compute_quotes(
            fair_value=0.52, fee_rate=0.072, vol_annual=0.55,
            time_remaining_s=180.0, size_shares=10.0,
            inventory_skew_cents=0.0,
        )
    """

    name = "maker_quoting"

    def __init__(self, config: MakerQuotingConfig | None = None) -> None:
        self._config = config or MakerQuotingConfig()

    # ─────────────────────────────────────────────────────────────────
    #  Core compute
    # ─────────────────────────────────────────────────────────────────

    def compute_half_spread(
        self,
        *,
        fair_value: float,
        fee_rate: float,
        vol_annual: float,
        time_remaining_s: float,
    ) -> float:
        """Return the half-spread in price units.

        The half-spread is the larger of:
          - the explicit floor from config (`min_half_spread_cents`)
          - the fee+adverse selection minimum derived from the math.
        """
        # Fee component (price units): half-spread covers half the round-trip.
        fee_round_trip = 2.0 * fee_at_price(fair_value, theta=fee_rate)
        adverse = self._adverse_selection(vol_annual, time_remaining_s)
        derived = (fee_round_trip + adverse) / 2.0
        floor = self._config.min_half_spread_cents / 100.0
        return max(derived, floor)

    def _adverse_selection(
        self, vol_annual: float, time_remaining_s: float
    ) -> float:
        """Estimate price units of adverse-selection drift over the quote
        lifetime.

        We scale linearly with the vol regime relative to a reference (45%
        annualised) and dampen as expiry approaches: a quote 5s before
        resolution is barely exposed; one 4 minutes out is fully exposed.
        """
        base = self._config.adverse_selection_buffer_cents / 100.0
        vol_ratio = (vol_annual or self._config.reference_annual_vol) / max(
            self._config.reference_annual_vol, 1e-6
        )
        vol_scale = min(self._config.vol_scaling_max, max(0.25, vol_ratio))
        time_scale = min(
            1.0,
            max(0.0, time_remaining_s) / max(self._config.time_decay_horizon_s, 1e-6),
        )
        return base * vol_scale * time_scale

    def compute_quotes(
        self,
        *,
        fair_value: float,
        fee_rate: float,
        vol_annual: float,
        time_remaining_s: float,
        size_shares: float,
        inventory_skew_cents: float = 0.0,
        net_inventory_shares: float = 0.0,
        max_inventory_shares: float = 0.0,
        trend_drift: float = 0.0,
    ) -> tuple[Quote, Quote]:
        """Return (yes_bid, no_bid). Raises NoQuote when neither side is viable.

        A side is *not viable* if its bid falls outside [0.01, 0.99] —
        Polymarket rejects ticks past the boundary.

        `inventory_skew_cents` shifts both sides in the same direction
        (positive = lean against long YES position).

        `net_inventory_shares` / `max_inventory_shares` enable inventory-aware
        one-sided quoting: once the position exceeds the soft limit, the side
        that would grow it is suppressed (size 0) so only reducing fills can
        occur. A YES BUY grows long YES; a NO BUY reduces it.

        `trend_drift` is the projected signed change in fair value over the
        quote lifetime (negative = fair falling). The side losing value is
        widened by the adverse drift and pulled entirely once it exceeds
        `trend_suppress_ratio` x half-spread.
        """
        if not (0.0 < fair_value < 1.0):
            raise NoQuoteError(f"fair_value {fair_value:.4f} outside (0,1)")
        if size_shares <= 0:
            raise NoQuoteError("size_shares must be positive")
        if not math.isfinite(fee_rate) or fee_rate < 0:
            raise NoQuoteError(f"fee_rate {fee_rate!r} invalid")

        half = self.compute_half_spread(
            fair_value=fair_value,
            fee_rate=fee_rate,
            vol_annual=vol_annual,
            time_remaining_s=time_remaining_s,
        )
        skew = inventory_skew_cents / 100.0

        # Trend filter: a falling fair (-drift) picks off the YES bid; a
        # rising fair (+drift) picks off the NO bid. Widen the exposed side by
        # the adverse drift, and flag it for suppression once the move exceeds
        # the spread cushion.
        adverse_yes = max(0.0, -trend_drift)
        adverse_no = max(0.0, trend_drift)
        trip = self._config.trend_suppress_ratio * half
        trend_suppress_yes = trip > 0 and adverse_yes >= trip
        trend_suppress_no = trip > 0 and adverse_no >= trip

        # YES bid = fair - half_spread - skew (skew>0 ⇒ lean against long YES);
        # widened further by sub-trip adverse drift.
        yes_bid = fair_value - half - skew - (0.0 if trend_suppress_yes else adverse_yes)
        # NO bid = (1 - fair) - half_spread + skew (the mirror).
        no_bid = (1.0 - fair_value) - half + skew - (0.0 if trend_suppress_no else adverse_no)

        # Inventory-aware one-sided suppression. Suppress whichever side would
        # ADD to an already-heavy position; keep quoting the reducing side so
        # fills flatten us instead of freezing at the cap.
        inv_suppress_yes, inv_suppress_no = self._inventory_suppression(
            net_inventory_shares, max_inventory_shares
        )
        suppress_yes = inv_suppress_yes or trend_suppress_yes
        suppress_no = inv_suppress_no or trend_suppress_no

        yes_q = None if suppress_yes else self._maybe_quote(
            yes_bid, size_shares, side_yes=True, fair_value=fair_value
        )
        no_q = None if suppress_no else self._maybe_quote(
            no_bid, size_shares, side_yes=False, fair_value=fair_value
        )
        if yes_q is None and no_q is None:
            # If suppression nuked the only viable side, that's intentional
            # (inventory-capped, or a strong trend on the exposed side) — not
            # an error condition, just a deliberate stand-down.
            if suppress_yes or suppress_no:
                raise NoQuoteError("suppressed (inventory/trend); no quotable side")
            raise NoQuoteError("both sides outside valid price range")
        return (
            yes_q
            or Quote(
                side_yes=True,
                is_buy=True,
                price=0.0,
                size=0.0,
                reference_fair=fair_value,
                reference_mid=fair_value,
            ),
            no_q
            or Quote(
                side_yes=False,
                is_buy=True,
                price=0.0,
                size=0.0,
                reference_fair=1.0 - fair_value,
                reference_mid=1.0 - fair_value,
            ),
        )

    def _inventory_suppression(
        self, net_inventory_shares: float, max_inventory_shares: float
    ) -> tuple[bool, bool]:
        """Return (suppress_yes, suppress_no).

        Beyond the soft limit we stop quoting the side that would grow the
        position: long YES (net > 0) suppresses the YES bid; short YES
        (net < 0, i.e. long NO) suppresses the NO bid. The reducing side
        keeps quoting so fills flatten us toward zero.
        """
        if max_inventory_shares <= 0:
            return False, False
        soft = self._config.inventory_soft_limit_ratio * max_inventory_shares
        if net_inventory_shares >= soft:
            return True, False   # heavy long YES → stop buying YES
        if net_inventory_shares <= -soft:
            return False, True   # heavy short YES (long NO) → stop buying NO
        return False, False

    @staticmethod
    def _maybe_quote(
        price: float,
        size: float,
        *,
        side_yes: bool,
        fair_value: float,
    ) -> Quote | None:
        # Polymarket tick is 1¢. Round down so we don't accidentally land on
        # a worse-than-floor price, then clamp.
        ticked = math.floor(price * 100.0) / 100.0
        if ticked < 0.01 or ticked > 0.99:
            return None
        ref_mid = fair_value if side_yes else (1.0 - fair_value)
        return Quote(
            side_yes=side_yes,
            is_buy=True,
            price=ticked,
            size=size,
            reference_fair=fair_value,
            reference_mid=ref_mid,
        )

    # ─────────────────────────────────────────────────────────────────
    #  Helper used by QuoteManager
    # ─────────────────────────────────────────────────────────────────

    def is_quote_stale(
        self,
        *,
        quote_reference_fair: float,
        current_fair: float,
        threshold_cents: float = 1.5,
    ) -> bool:
        """Should an outstanding quote be cancelled because fair has drifted?

        Key insight from the brief: key off PRICE deviation, not TIME.
        A 5-second-old quote at the right price is fine; a 100ms-old quote
        at a stale price is dangerous.
        """
        drift = abs(current_fair - quote_reference_fair)
        return drift >= (threshold_cents / 100.0)
