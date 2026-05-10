"""Tests for RiskManager — including v3 patches and v4 projected-exposure check."""

from __future__ import annotations

import pytest

from polybot.data.models import (
    Direction,
    Position,
    PositionStatus,
    Side,
)
from polybot.risk.manager import RiskManager


# ─────────────────────────────────────────────────────────────────────────────
#  Sizing tests
# ─────────────────────────────────────────────────────────────────────────────


class TestKellySizing:
    def test_basic_kelly(self, risk_config, make_signal):
        rm = RiskManager(risk_config)
        sig = make_signal(
            target_price=0.50,
            fair_value=0.55,
            edge_bps=50.0,
            confidence=0.60,
        )
        result = rm.kelly_size_for_signal(sig, bankroll=500.0)
        assert result.size_usd > 0
        assert result.size_usd <= risk_config.max_position_size

    def test_floor_recovery_when_kelly_zero(self, risk_config, make_signal):
        """Kelly outputs 0 but edge is above floor — should snap to min_usd."""
        rm = RiskManager(risk_config)
        sig = make_signal(
            target_price=0.50,
            fair_value=0.501,  # tiny fair-value edge
            edge_bps=4.0,  # > edge_floor_bps=3
            confidence=0.05,  # low conf forces small Kelly
        )
        result = rm.kelly_size_for_signal(sig, bankroll=500.0)
        # Should never be 0 — should snap to either min_floor or floor_recovery
        assert result.size_usd >= risk_config.min_usd

    def test_legacy_path_no_metadata(self, risk_config, make_signal):
        """Signal without fair_value/edge_bps should still produce a size."""
        rm = RiskManager(risk_config)
        sig = make_signal(
            edge_bps=0.0,  # forces legacy path
            fair_value=0.0,
        )
        result = rm.kelly_size_for_signal(sig, bankroll=500.0)
        assert result.size_usd > 0

    def test_min_usd_floor(self, risk_config, make_signal):
        """Sized amount below min_usd should be floored to min_usd."""
        small_cfg = risk_config.model_copy(update={"min_usd": 5.0})
        rm = RiskManager(small_cfg)
        sig = make_signal(
            target_price=0.50,
            fair_value=0.501,
            edge_bps=5.0,
            confidence=0.10,
        )
        result = rm.kelly_size_for_signal(sig, bankroll=500.0)
        assert result.size_usd >= 5.0

    def test_max_position_size_cap(self, risk_config, make_signal):
        rm = RiskManager(risk_config)
        sig = make_signal(
            target_price=0.50,
            fair_value=0.85,  # huge edge
            edge_bps=3500.0,
            confidence=0.95,
        )
        result = rm.kelly_size_for_signal(sig, bankroll=10_000.0)
        assert result.size_usd <= risk_config.max_position_size

    def test_min_usd_default_when_unset(self):
        """min_usd default should be 2.0 even on a fresh RiskConfig."""
        from polybot.config import RiskConfig
        cfg = RiskConfig()
        rm = RiskManager(cfg)
        assert rm.min_usd == 2.0


# ─────────────────────────────────────────────────────────────────────────────
#  Entry gate (can_open_position) tests
# ─────────────────────────────────────────────────────────────────────────────


class TestCanOpenPosition:
    def test_under_caps_allows(self, risk_config, make_signal):
        from polybot.data.models import Portfolio
        rm = RiskManager(risk_config)
        portfolio = Portfolio()
        sig = make_signal()
        ok, reason = rm.can_open_position(portfolio, sig, projected_notional=20.0)
        assert ok is True

    def test_position_count_cap(self, risk_config, make_signal, make_market):
        from polybot.data.models import Portfolio
        rm = RiskManager(risk_config)
        portfolio = Portfolio()
        # Fill to max_positions
        for i in range(risk_config.max_positions):
            portfolio.positions.append(
                Position(
                    market_id=f"m{i}",
                    token_id=f"t{i}",
                    side=Side.BUY,
                    size=10,
                    avg_entry_price=0.5,
                    strategy="test",
                    status=PositionStatus.OPEN,
                )
            )
        sig = make_signal()
        ok, reason = rm.can_open_position(portfolio, sig)
        assert ok is False
        assert "position_cap" in reason

    def test_projected_exposure_blocks_when_over_cap(self, risk_config, make_signal):
        """Current exposure under cap, but projected goes over — should reject."""
        from polybot.data.models import Portfolio
        rm = RiskManager(risk_config)
        portfolio = Portfolio()
        # Existing position taking 95% of cap
        portfolio.positions.append(
            Position(
                market_id="m-existing",
                token_id="t1",
                side=Side.BUY,
                size=380,
                avg_entry_price=0.5,
                strategy="test",
                status=PositionStatus.OPEN,
            )
        )
        # 380 × 0.5 = 190, cap = 200 → only $10 headroom
        sig = make_signal()
        ok, reason = rm.can_open_position(portfolio, sig, projected_notional=50.0)
        assert ok is False
        assert "exposure" in reason.lower()

    def test_dual_leg_signal_reserves_two_position_slots(
        self, risk_config, make_signal,
    ):
        """A dual-direction signal opens YES+NO legs in one shot. The cap
        check must reserve both slots up front, otherwise a single dual
        signal can push the portfolio past max_positions and lock the bot
        out (held-to-expiry legs never free the slots until auto-close).
        """
        from polybot.data.models import Portfolio
        rm = RiskManager(risk_config)
        portfolio = Portfolio()
        # Fill to max_positions - 1 (one slot free)
        for i in range(risk_config.max_positions - 1):
            portfolio.positions.append(
                Position(
                    market_id=f"m{i}",
                    token_id=f"t{i}",
                    side=Side.BUY,
                    size=10,
                    avg_entry_price=0.5,
                    strategy="test",
                    status=PositionStatus.OPEN,
                )
            )
        sig = make_signal(strategy="dual_direction_arb")
        # A 1-leg signal would still fit (1 free slot).
        ok, _ = rm.can_open_position(portfolio, sig, projected_positions=1)
        assert ok is True
        # A 2-leg signal must be rejected: 5 + 2 > 6.
        ok, reason = rm.can_open_position(portfolio, sig, projected_positions=2)
        assert ok is False
        assert "position_cap" in reason


# ─────────────────────────────────────────────────────────────────────────────
#  Order-level gate (can_place_order) tests
# ─────────────────────────────────────────────────────────────────────────────


class TestCanPlaceOrder:
    def test_normal_order_passes(self, risk_config, make_order):
        from polybot.data.models import Portfolio
        rm = RiskManager(risk_config)
        portfolio = Portfolio()
        order = make_order(price=0.5, size=20)  # $10 notional
        ok, _ = rm.can_place_order(order, portfolio)
        assert ok

    def test_zero_size_rejected(self, risk_config, make_order):
        from polybot.data.models import Portfolio
        rm = RiskManager(risk_config)
        order = make_order(price=0.5, size=0)
        ok, reason = rm.can_place_order(order, Portfolio())
        assert ok is False
        assert "zero" in reason

    def test_max_order_size_rejects(self, risk_config, make_order):
        from polybot.data.models import Portfolio
        rm = RiskManager(risk_config)
        # max_order_size = 25 in fixture; 100 × 0.5 = $50
        order = make_order(price=0.5, size=100)
        ok, reason = rm.can_place_order(order, Portfolio())
        assert ok is False
        assert "max_order_size" in reason

    def test_exit_order_bypasses_gates(self, risk_config, make_order):
        """Exit orders must always go through, even if oversized."""
        from polybot.data.models import Portfolio
        rm = RiskManager(risk_config)
        order = make_order(price=0.5, size=200, strategy="exit_overshoot_reversion")
        ok, _ = rm.can_place_order(order, Portfolio())
        assert ok

    def test_auto_exit_also_bypasses(self, risk_config, make_order):
        from polybot.data.models import Portfolio
        rm = RiskManager(risk_config)
        order = make_order(
            price=0.5, size=200,
            strategy="auto_exit_dual_direction_arb",
        )
        ok, _ = rm.can_place_order(order, Portfolio())
        assert ok

    def test_projected_exposure_in_can_place(self, risk_config, make_order):
        from polybot.data.models import Portfolio
        rm = RiskManager(risk_config)
        portfolio = Portfolio()
        portfolio.positions.append(
            Position(
                market_id="m-x",
                token_id="t",
                side=Side.BUY,
                size=380,
                avg_entry_price=0.5,
                strategy="test",
                status=PositionStatus.OPEN,
            )
        )
        order = make_order(price=0.5, size=40)  # $20 notional, would go to 210 > 200 cap
        ok, reason = rm.can_place_order(order, portfolio)
        assert ok is False
        assert "exposure" in reason.lower()


# ─────────────────────────────────────────────────────────────────────────────
#  Daily-loss halt
# ─────────────────────────────────────────────────────────────────────────────


def test_daily_loss_halt(risk_config, make_signal):
    from polybot.data.models import Portfolio
    rm = RiskManager(risk_config)
    rm.record_pnl("overshoot_reversion", -25.0)
    sig = make_signal()
    ok, reason = rm.can_open_position(Portfolio(), sig)
    assert ok is False
    assert "daily_loss_halt" in reason
