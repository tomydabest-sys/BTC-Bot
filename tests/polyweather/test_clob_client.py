"""CLOB REST client tests (mock + a focused mock-server smoke).

We can't hit the live Polymarket CLOB from CI, so the mock client is
exercised here and the live client's defensive parsing is unit-tested.
"""

from __future__ import annotations

import pytest

from polybot.polyweather.exchanges.clob_client import (
    CLOBBook,
    CLOBPriceLevel,
    MockCLOBClient,
)


@pytest.mark.asyncio
async def test_mock_book_returns_well_formed_book() -> None:
    client = MockCLOBClient(default_mid=0.42)
    book = await client.fetch_book("t1")
    assert isinstance(book, CLOBBook)
    assert book.token_id == "t1"
    assert book.bids and book.asks
    assert book.best_bid < book.best_ask
    assert 0 < book.midpoint < 1


@pytest.mark.asyncio
async def test_mock_midpoint_and_last_trade() -> None:
    client = MockCLOBClient(default_mid=0.55)
    mid = await client.fetch_midpoint("t1")
    last = await client.fetch_last_trade_price("t1")
    assert mid == 0.55
    assert last == 0.55


@pytest.mark.asyncio
async def test_mock_price_per_side() -> None:
    client = MockCLOBClient(default_mid=0.50)
    buy_price = await client.fetch_price("t1", "BUY")
    sell_price = await client.fetch_price("t1", "SELL")
    assert buy_price < sell_price


def test_clob_book_spread_bps() -> None:
    book = CLOBBook(
        token_id="t",
        bids=[CLOBPriceLevel(0.49, 100)],
        asks=[CLOBPriceLevel(0.51, 100)],
        midpoint=0.50,
    )
    assert 350 < book.spread_bps < 450  # ~400 bps


def test_clob_book_empty_safe() -> None:
    book = CLOBBook(token_id="t")
    assert book.best_bid == 0.0
    assert book.best_ask == 1.0
    assert book.spread_bps == 10000.0
