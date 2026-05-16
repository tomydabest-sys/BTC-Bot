"""Bot lifecycle integration test with maker mode enabled.

Exercises the wiring in main.Bot.__init__ + start() + stop() — proves
that flipping config.maker.enabled=True actually constructs the
MakerOrchestrator + TelegramAlerter and runs them under the same
lifecycle as the rest of the bot, without touching the network.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from polybot.config import (
    BotConfig,
    ClobV2Config,
    Config,
    DecisionLogConfig,
    DeploymentConfig,
    ExecutionConfig,
    FeaturesRetentionConfig,
    MakerConfig,
    MonitoringConfig,
    RiskConfig,
    ScannerConfig,
    StrategiesConfig,
    WalletConfig,
)
from polybot.data.models import Market, OrderBook, PriceLevel


@pytest.fixture
def temp_data_dir(tmp_path):
    return str(tmp_path / "data")


@pytest.fixture
def maker_enabled_config(temp_data_dir):
    return Config(
        bot=BotConfig(mode="paper", data_dir=temp_data_dir),
        wallet=WalletConfig(),
        scanner=ScannerConfig(interval_seconds=30, btc_updown_only=True),
        strategies=StrategiesConfig(enabled=[]),
        risk=RiskConfig(bankroll_usd=500),
        execution=ExecutionConfig(loop_interval_ms=200),
        monitoring=MonitoringConfig(),
        decision_log=DecisionLogConfig(
            path=os.path.join(temp_data_dir, "decisions.jsonl")
        ),
        features_retention=FeaturesRetentionConfig(enabled=False),
        clob=ClobV2Config(),
        maker=MakerConfig(
            enabled=True,
            primary_markets=["btc-5m"],
            target_size_shares=5,
            binance_stale_threshold_s=10.0,
        ),
        deployment=DeploymentConfig(heartbeat_interval_s=99999.0),
    )


@pytest.fixture
def injected_market() -> Market:
    return Market(
        id="m-test-5m-1",
        question="Bitcoin Up or Down — testing window?",
        slug="btc-updown-5m-1700000000",
        outcomes=["Up", "Down"],
        token_ids=["tok-yes", "tok-no"],
        end_date=datetime.now(UTC) + timedelta(minutes=4),
        category="crypto",
        active=True,
        volume_24h=10_000,
        liquidity=500,
    )


def _make_orderbook(market_id: str, mid: float = 0.50) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        timestamp=datetime.now(UTC),
        bids=[PriceLevel(price=mid - 0.01, size=100)],
        asks=[PriceLevel(price=mid + 0.01, size=100)],
    )


@pytest.mark.asyncio
async def test_bot_starts_with_maker_enabled_and_runs_clean(
    maker_enabled_config, injected_market, monkeypatch
):
    """Smoke test: bot starts, maker subsystem is wired, bot stops clean."""
    # Force mock BTC feed so we don't try to dial Binance.
    monkeypatch.setenv("BTC_BOT_USE_MOCK_FEED", "1")

    from polybot.main import Bot

    bot = Bot(maker_enabled_config)
    assert bot.maker is not None, "maker orchestrator must exist"

    # Replace network-touching components with fakes BEFORE start().
    # The real ones would try to dial Polymarket and explode in the sandbox.
    bot._client = MagicMock()
    bot._client.start = AsyncMock()
    bot._client.close = AsyncMock()
    bot._client.get_markets = AsyncMock(return_value=[])
    bot._client.get_orderbook = AsyncMock(return_value=None)
    bot._ws.start = AsyncMock()
    bot._ws.stop = AsyncMock()
    bot._ws.subscribe_market = AsyncMock()
    bot._ws.unsubscribe_market = AsyncMock()
    bot._scanner.start = AsyncMock()
    bot._scanner.stop = AsyncMock()
    bot._pipeline.start = AsyncMock()
    bot._pipeline.stop = AsyncMock()

    # Inject a market directly so the scanner appears to have one.
    # active_markets is a *copy*; we need to mutate the underlying dict.
    bot._scanner._active_markets[injected_market.id] = injected_market

    # Pipeline returns a stable snapshot for the injected market.
    fake_snap = MagicMock()
    fake_snap.market = injected_market
    fake_snap.orderbook = _make_orderbook(injected_market.id, mid=0.50)
    bot._pipeline.get_snapshot = MagicMock(return_value=fake_snap)
    bot._pipeline.register_market = MagicMock()
    bot._pipeline.unregister_market = MagicMock()
    bot._pipeline.get_market = MagicMock(return_value=injected_market)

    await bot.start()
    try:
        # Let the maker loop run several times — needs the mock feed to
        # publish at least one tick, plus a maintain_states cycle, plus
        # a sync_all cycle.
        for _ in range(20):
            await asyncio.sleep(0.2)
            if bot.maker.markets:
                break
        v = bot.maker.vitals()
        assert v.active_markets == 1, (
            f"maker should have registered market within 4s; "
            f"vitals={v}, scanner_markets={list(bot.scanner.active_markets)}, "
            f"feed_price={bot.exchange_feed.last_price}"
        )
        # Wait one more sync cycle for quotes to be placed
        for _ in range(10):
            await asyncio.sleep(0.2)
            state = bot.maker.markets[injected_market.id]
            if state.quote_manager.state.resting:
                break
        state = bot.maker.markets[injected_market.id]
        assert len(state.quote_manager.state.resting) >= 1, (
            f"expected resting quotes; state={state}"
        )
    finally:
        await bot.stop()


@pytest.mark.asyncio
async def test_bot_without_maker_does_not_init_orchestrator(temp_data_dir):
    """maker.enabled=False must keep MakerOrchestrator None."""
    cfg = Config(
        bot=BotConfig(mode="paper", data_dir=temp_data_dir),
        maker=MakerConfig(enabled=False),
    )

    from polybot.main import Bot

    bot = Bot(cfg)
    assert bot.maker is None
