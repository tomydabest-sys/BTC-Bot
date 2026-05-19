"""Tests for the honest decision-gate accounting.

Before this change, strategies logged `reason="ok"` the moment they
produced a candidate signal — but a signal only becomes a trade after
the aggregator's min_net_score gate, the risk gate, and execution all
pass. The dashboard's "trade rate" therefore counted proposals, not
trades, and a bot that fired 118 signals/hr while executing zero looked
healthy.

Now: strategies emit `signal_proposed`; only an actually-executed trade
emits `ok`. The analyzer surfaces the gap as a conversion-gate warning.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from polybot.analyze import health_check
from polybot.data.models import Market, MarketSnapshot, OrderBook, PriceLevel
from polybot.diagnostics import decision_log
from polybot.diagnostics.decision_log import BlockReason
from polybot.strategies.maker_edge import MakerEdgeStrategy


def _imbalanced_snapshot(mid: float = 0.50) -> MarketSnapshot:
    """A book with heavy bid-side imbalance so maker_edge picks a side."""
    market = Market(
        id="m-1",
        question="Bitcoin Up or Down — 12:30AM-12:35AM?",
        slug="btc-updown-5m-1",
        outcomes=["Up", "Down"],
        token_ids=["tok-yes", "tok-no"],
        end_date=datetime.utcnow() + timedelta(seconds=120),
        category="crypto",
        active=True,
        volume_24h=5000.0,
        liquidity=200.0,
    )
    ob = OrderBook(
        market_id="m-1",
        timestamp=datetime.utcnow(),
        bids=[PriceLevel(price=mid - 0.01, size=100.0)],
        asks=[PriceLevel(price=mid + 0.01, size=10.0)],  # thin ask → imbalance
    )
    return MarketSnapshot(market=market, orderbook=ob, recent_trades=[], poly_move_5s=0.0)


@pytest.mark.asyncio
async def test_strategy_signal_emits_signal_proposed_not_ok():
    """A maker_edge candidate must log `signal_proposed`, never `ok` — the
    strategy is upstream of the conversion gate, so it cannot know yet
    whether the signal will execute."""
    decision_log.reset_counter()
    strat = MakerEdgeStrategy()
    snap = _imbalanced_snapshot()
    sig = await strat.evaluate(snap)
    assert sig is not None, "crafted snapshot should produce a maker_edge signal"
    counts = decision_log.block_summary(top_n=50)
    assert counts.get(BlockReason.SIGNAL_PROPOSED, 0) >= 1
    assert counts.get(BlockReason.OK, 0) == 0, (
        "a strategy must NOT emit `ok` — only an executed trade does"
    )


def test_analyze_flags_conversion_gate_starvation():
    """proposed > 0 and ok == 0 is the user's exact bug: strategies fire,
    nothing converts. health_check must surface it as CRITICAL."""
    decisions = [
        {"reason": "signal_proposed", "strategy": "maker_edge", "ts": float(i)}
        for i in range(200)
    ]
    warnings = health_check(decisions)
    joined = " ".join(warnings)
    assert "PROPOSED" in joined and "0 EXECUTED" in joined, (
        f"expected a conversion-gate warning, got: {warnings}"
    )


def test_analyze_no_starvation_warning_when_trades_execute():
    decisions = [
        {"reason": "signal_proposed", "strategy": "maker_edge", "ts": float(i)}
        for i in range(150)
    ] + [
        {"reason": "ok", "strategy": "maker_edge", "ts": float(150 + i)}
        for i in range(20)
    ]
    warnings = health_check(decisions)
    joined = " ".join(warnings)
    assert "0 EXECUTED" not in joined


def test_analyze_flags_total_silence():
    """No proposals and no trades at all is the *other* failure mode and
    must produce a distinct message."""
    decisions = [
        {"reason": "no_feed", "strategy": "overshoot_reversion", "ts": float(i)}
        for i in range(200)
    ]
    warnings = health_check(decisions)
    joined = " ".join(warnings)
    assert "0 OK + 0 proposed" in joined
