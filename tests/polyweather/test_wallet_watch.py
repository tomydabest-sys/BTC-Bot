"""Wallet-watch poller — tracked-trader confirmation signal.

Pins that the watcher (a) filters to TRADE activity within the lookback
window, (b) computes per-market bucket-bullish flow (BUY Yes / SELL No is
bullish; BUY No / SELL Yes is bearish), (c) counts distinct wallets per
market, and (d) is a no-op with no wallets configured.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from polybot.polyweather.exchanges.data_api_client import MockDataApiClient, WalletActivity
from polybot.polyweather.exchanges.wallet_watch import WalletWatcher

NOW = 1_780_000_000


def _act(
    *, cid: str, side: str, outcome: str, usdc: str, ts: int = NOW, type_: str = "TRADE",
    title: str = "Will the highest temperature in Miami be 84-85°F on May 29?",
    slug: str = "highest-temperature-in-miami-on-may-29-2026-84-85f",
) -> WalletActivity:
    return WalletActivity(
        timestamp=ts, type=type_, side=side, asset="a", condition_id=cid, outcome=outcome,
        price=Decimal("0.5"), size=Decimal("10"), usdc_size=Decimal(usdc),
        title=title, slug=slug, event_slug=slug, tx_hash="0x",
    )


def _factory(mapping: dict[str, list[WalletActivity]]):
    return lambda addr: MockDataApiClient(activity=mapping.get(addr.lower(), []))


@pytest.mark.asyncio
async def test_bullish_market_aggregates_across_wallets():
    a1 = "0xaaa"
    a2 = "0xbbb"
    mapping = {
        a1: [_act(cid="mkt-miami", side="BUY", outcome="Yes", usdc="100")],
        a2: [_act(cid="mkt-miami", side="SELL", outcome="No", usdc="50")],  # also bullish
    }
    w = WalletWatcher(
        [{"address": a1, "label": "one"}, {"address": a2, "label": "two"}],
        client_factory=_factory(mapping),
    )
    snap = await w.poll(now=NOW)

    assert len(snap.market_signals) == 1
    sig = snap.market_signals[0]
    assert sig.condition_id == "mkt-miami"
    assert sig.wallets == 2
    assert sig.net_usdc == Decimal("150")    # 100 + 50, both bullish
    assert sig.direction == "bullish"
    # per-wallet net flow
    by_addr = {wsn.address: wsn for wsn in snap.wallets}
    assert by_addr["0xaaa"].net_usdc == Decimal("100")
    assert by_addr["0xbbb"].net_usdc == Decimal("50")


@pytest.mark.asyncio
async def test_buy_no_is_bearish():
    a1 = "0xaaa"
    mapping = {a1: [_act(cid="m", side="BUY", outcome="No", usdc="80")]}
    w = WalletWatcher([{"address": a1, "label": "one"}], client_factory=_factory(mapping))
    snap = await w.poll(now=NOW)
    assert snap.market_signals[0].direction == "bearish"
    assert snap.market_signals[0].net_usdc == Decimal("-80")


@pytest.mark.asyncio
async def test_stale_and_non_trade_activity_is_ignored():
    a1 = "0xaaa"
    mapping = {
        a1: [
            _act(cid="m", side="BUY", outcome="Yes", usdc="10", ts=NOW - 10 * 86400),  # stale
            _act(cid="m", side="BUY", outcome="Yes", usdc="20", type_="REDEEM"),        # not a trade
            _act(cid="m", side="BUY", outcome="Yes", usdc="30"),                        # counts
        ]
    }
    w = WalletWatcher(
        [{"address": a1, "label": "one"}],
        client_factory=_factory(mapping),
        lookback_seconds=86400,
    )
    snap = await w.poll(now=NOW)
    assert snap.wallets[0].weather_trades == 1
    assert snap.market_signals[0].net_usdc == Decimal("30")


@pytest.mark.asyncio
async def test_no_wallets_is_disabled_noop():
    w = WalletWatcher([])
    assert w.enabled is False
    snap = await w.poll(now=NOW)
    assert snap.wallets == []
    assert snap.market_signals == []


@pytest.mark.asyncio
async def test_poll_failure_is_isolated_per_wallet():
    a1, a2 = "0xaaa", "0xbbb"

    class _Boom:
        async def weather_activity(self, limit=100):
            raise RuntimeError("data-api down")

    def factory(addr):
        if addr == a1:
            return _Boom()
        return MockDataApiClient(activity=[_act(cid="m", side="BUY", outcome="Yes", usdc="40")])

    w = WalletWatcher(
        [{"address": a1, "label": "broken"}, {"address": a2, "label": "ok"}],
        client_factory=factory,
    )
    snap = await w.poll(now=NOW)
    by_addr = {wsn.address: wsn for wsn in snap.wallets}
    assert by_addr["0xaaa"].error  # captured, not raised
    assert by_addr["0xbbb"].weather_trades == 1
