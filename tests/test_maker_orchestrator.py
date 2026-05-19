"""End-to-end integration test for the maker orchestrator (paper mode)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from polybot.config import MakerConfig
from polybot.data.exchange_feed import PriceFeedState
from polybot.data.models import Market, OrderBook, PriceLevel
from polybot.execution.maker_orchestrator import MakerOrchestrator

# ─────────────────────────────────────────────────────────────────────────────
#  Fakes
# ─────────────────────────────────────────────────────────────────────────────


class _FakeScanner:
    def __init__(self, markets: dict[str, Market]) -> None:
        self.active_markets = markets


class _FakeSnapshot:
    def __init__(self, market: Market, mid: float) -> None:
        self.market = market
        ts = datetime.now(UTC)
        spread = 0.02
        self.orderbook = OrderBook(
            market_id=market.id,
            timestamp=ts,
            bids=[PriceLevel(price=mid - spread / 2, size=100)],
            asks=[PriceLevel(price=mid + spread / 2, size=100)],
        )


class _FakePipeline:
    def __init__(self) -> None:
        self._mids: dict[str, float] = {}

    def set_mid(self, market_id: str, mid: float) -> None:
        self._mids[market_id] = mid

    def get_snapshot(self, market_id: str):
        return None  # caller never reads through here; orchestrator does

    def get_snapshot_with_market(
        self, market: Market
    ) -> _FakeSnapshot:
        return _FakeSnapshot(market, self._mids.get(market.id, 0.5))


# We want the orchestrator to call pipeline.get_snapshot(market_id), so
# wire the actual call through a proxy that knows the market.
class _PipelineProxy:
    def __init__(self, inner: _FakePipeline, markets: dict[str, Market]) -> None:
        self._inner = inner
        self._markets = markets

    def get_snapshot(self, market_id: str):
        m = self._markets.get(market_id)
        if m is None:
            return None
        return self._inner.get_snapshot_with_market(m)


class _FakeFeed:
    """Minimal exchange-feed surface the orchestrator pokes at."""

    def __init__(self, start_price: float = 95_000.0) -> None:
        self._state = PriceFeedState()
        self._state.push(start_price)

    def push(self, price: float) -> None:
        self._state.push(price)

    @property
    def last_price(self) -> float:
        return self._state.last_price

    @property
    def feed_age_s(self) -> float | None:
        if not self._state.ticks:
            return None
        return max(0.0, time.time() - self._state.ticks[-1].timestamp)


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def market_5m() -> Market:
    return Market(
        id="m-5m-1",
        question="Bitcoin Up or Down — 12:30-12:35AM?",
        slug="btc-updown-5m-1700000000",
        outcomes=["Up", "Down"],
        token_ids=["tok-yes", "tok-no"],
        end_date=datetime.now(UTC) + timedelta(minutes=4),
        category="crypto",
        active=True,
        volume_24h=10_000,
        liquidity=500,
    )


@pytest.fixture
def maker_cfg() -> MakerConfig:
    return MakerConfig(
        enabled=True,
        primary_markets=["btc-5m"],
        min_half_spread_cents=2.0,
        target_size_shares=5,
        max_quote_lifetime_s=30,
        requote_threshold_cents=1.5,
        flatten_before_expiry_s=10,
        binance_stale_threshold_s=2.0,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_places_quotes_on_first_tick(market_5m, maker_cfg):
    markets = {market_5m.id: market_5m}
    scanner = _FakeScanner(markets)
    pipeline_inner = _FakePipeline()
    pipeline_inner.set_mid(market_5m.id, 0.50)
    pipeline = _PipelineProxy(pipeline_inner, markets)
    feed = _FakeFeed(start_price=95_000.0)

    orch = MakerOrchestrator(
        maker_cfg=maker_cfg,
        scanner=scanner,
        pipeline=pipeline,
        exchange_feed=feed,
        is_paper=True,
        client=None,
        telegram=None,
    )

    await orch.start()
    try:
        # Let the loop run a few iterations.
        await asyncio.sleep(0.6)
        assert market_5m.id in orch.markets
        state = orch.markets[market_5m.id]
        # Paper mode synthesises acks; resting orders should exist.
        assert len(state.quote_manager.state.resting) >= 1
        v = orch.vitals()
        assert v.active_markets == 1
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_orchestrator_ignores_non_btc_markets(maker_cfg):
    other = Market(
        id="m-eth-1",
        question="ETH up or down?",
        slug="eth-updown-1h-1700000000",
        outcomes=["Up", "Down"],
        token_ids=["tok-yes", "tok-no"],
        end_date=datetime.now(UTC) + timedelta(minutes=4),
        category="crypto",
        active=True,
    )
    markets = {other.id: other}
    scanner = _FakeScanner(markets)
    pipeline = _PipelineProxy(_FakePipeline(), markets)
    feed = _FakeFeed()

    orch = MakerOrchestrator(
        maker_cfg=maker_cfg,
        scanner=scanner,
        pipeline=pipeline,
        exchange_feed=feed,
        is_paper=True,
        client=None,
        telegram=None,
    )
    await orch.start()
    try:
        await asyncio.sleep(0.4)
        assert orch.markets == {}, "ETH market should not be picked up"
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_orchestrator_drops_market_when_scanner_removes_it(
    market_5m, maker_cfg
):
    markets = {market_5m.id: market_5m}
    scanner = _FakeScanner(markets)
    pipeline_inner = _FakePipeline()
    pipeline_inner.set_mid(market_5m.id, 0.50)
    pipeline = _PipelineProxy(pipeline_inner, markets)
    feed = _FakeFeed()

    orch = MakerOrchestrator(
        maker_cfg=maker_cfg,
        scanner=scanner,
        pipeline=pipeline,
        exchange_feed=feed,
        is_paper=True,
        client=None,
        telegram=None,
    )
    await orch.start()
    try:
        await asyncio.sleep(0.4)
        assert market_5m.id in orch.markets
        # Scanner removes the market
        scanner.active_markets = {}
        markets.clear()
        await asyncio.sleep(0.4)
        assert market_5m.id not in orch.markets
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_orchestrator_simulates_paper_fills_on_mid_cross(
    market_5m, maker_cfg
):
    markets = {market_5m.id: market_5m}
    scanner = _FakeScanner(markets)
    pipeline_inner = _FakePipeline()
    pipeline_inner.set_mid(market_5m.id, 0.50)
    pipeline = _PipelineProxy(pipeline_inner, markets)
    feed = _FakeFeed()

    orch = MakerOrchestrator(
        maker_cfg=maker_cfg,
        scanner=scanner,
        pipeline=pipeline,
        exchange_feed=feed,
        is_paper=True,
        client=None,
        telegram=None,
    )
    await orch.start()
    try:
        # Wait for first sync to place quotes
        await asyncio.sleep(0.4)
        state = orch.markets[market_5m.id]
        assert len(state.quote_manager.state.resting) >= 1
        # Crash the mid downward so the YES bid prices get crossed.
        pipeline_inner.set_mid(market_5m.id, 0.20)
        await asyncio.sleep(0.6)
        # Some fills should have happened on the YES side
        assert state.fills_today >= 1
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_orchestrator_cancels_all_on_feed_disconnect(
    market_5m, maker_cfg
):
    # Override threshold to fire quickly
    cfg = maker_cfg.model_copy(update={"binance_stale_threshold_s": 0.5})

    markets = {market_5m.id: market_5m}
    scanner = _FakeScanner(markets)
    pipeline_inner = _FakePipeline()
    pipeline_inner.set_mid(market_5m.id, 0.50)
    pipeline = _PipelineProxy(pipeline_inner, markets)
    feed = _FakeFeed()

    orch = MakerOrchestrator(
        maker_cfg=cfg,
        scanner=scanner,
        pipeline=pipeline,
        exchange_feed=feed,
        is_paper=True,
        client=None,
        telegram=None,
    )
    await orch.start()
    try:
        await asyncio.sleep(0.4)
        state = orch.markets[market_5m.id]
        assert len(state.quote_manager.state.resting) >= 1
        # Stop pushing ticks; the watchdog should fire within ~1s
        await asyncio.sleep(2.0)
        assert len(state.quote_manager.state.resting) == 0
    finally:
        await orch.stop()


@pytest.mark.asyncio
async def test_vitals_aggregate_correctly(market_5m, maker_cfg):
    markets = {market_5m.id: market_5m}
    scanner = _FakeScanner(markets)
    pipeline_inner = _FakePipeline()
    pipeline_inner.set_mid(market_5m.id, 0.50)
    pipeline = _PipelineProxy(pipeline_inner, markets)
    feed = _FakeFeed()

    orch = MakerOrchestrator(
        maker_cfg=maker_cfg,
        scanner=scanner,
        pipeline=pipeline,
        exchange_feed=feed,
        is_paper=True,
        client=None,
        telegram=None,
    )
    await orch.start()
    try:
        await asyncio.sleep(0.6)
        v = orch.vitals()
        assert v.active_markets == 1
        assert v.quote_uptime_pct >= 0
        assert v.p95_latency_ms >= 0
    finally:
        await orch.stop()


# ─────────────────────────────────────────────────────────────────────────────
#  Regression: mixed tz-awareness between Gamma end_date (aware) and pipeline
#  orderbook timestamp (naive) used to throw on every loop iteration, which
#  also poisoned the validation gate's unhandled_exceptions counter.
# ─────────────────────────────────────────────────────────────────────────────


class _NaiveTimestampSnapshot:
    """Mirrors production: pipeline stamps orderbooks with naive utcnow()."""

    def __init__(self, market: Market, mid: float) -> None:
        self.market = market
        spread = 0.02
        self.orderbook = OrderBook(
            market_id=market.id,
            timestamp=datetime.utcnow(),  # NAIVE, like the real pipeline
            bids=[PriceLevel(price=mid - spread / 2, size=100)],
            asks=[PriceLevel(price=mid + spread / 2, size=100)],
        )


class _NaivePipelineProxy:
    def __init__(self, markets: dict[str, Market], mid: float) -> None:
        self._markets = markets
        self._mid = mid

    def get_snapshot(self, market_id: str):
        m = self._markets.get(market_id)
        if m is None:
            return None
        return _NaiveTimestampSnapshot(m, self._mid)


@pytest.mark.asyncio
async def test_mixed_tz_does_not_throw_or_poison_validation_gate(
    market_5m, maker_cfg, tmp_path
):
    from polybot.monitoring.paper_validation import PaperValidationGate

    # Gamma-style aware end_date + naive orderbook timestamp.
    aware_market = market_5m  # fixture already uses datetime.now(UTC)
    assert aware_market.end_date.tzinfo is not None

    markets = {aware_market.id: aware_market}
    scanner = _FakeScanner(markets)
    pipeline = _NaivePipelineProxy(markets, mid=0.50)
    feed = _FakeFeed()
    gate = PaperValidationGate(
        state_path=str(tmp_path / "v.json"), starting_equity_usd=500.0
    )

    orch = MakerOrchestrator(
        maker_cfg=maker_cfg,
        scanner=scanner,
        pipeline=pipeline,
        exchange_feed=feed,
        is_paper=True,
        client=None,
        telegram=None,
        validation_gate=gate,
    )
    await orch.start()
    try:
        await asyncio.sleep(0.6)
        # Before the fix this raised TypeError every iteration, recording an
        # unhandled exception each time and placing zero quotes.
        assert gate.state.unhandled_exceptions == 0, (
            "mixed-tz subtraction must not raise into the loop's except handler"
        )
        state = orch.markets[aware_market.id]
        assert len(state.quote_manager.state.resting) >= 1, (
            "quotes should be placed once t_rem computes cleanly"
        )
    finally:
        await orch.stop()
