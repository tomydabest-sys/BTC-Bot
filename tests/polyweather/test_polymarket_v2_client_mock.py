"""MockPolymarketV2Client: deterministic IDs, heartbeat task, V2 constants."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from polybot.polyweather.exchanges.polymarket_v2_client import (
    BATCH_ORDER_SIZE,
    EIP712_DOMAIN_VERSION,
    MockPolymarketV2Client,
    V2Order,
)


def test_eip712_domain_version_is_string_two() -> None:
    assert EIP712_DOMAIN_VERSION == "2"
    assert isinstance(EIP712_DOMAIN_VERSION, str)


def test_batch_size_is_fifteen() -> None:
    assert BATCH_ORDER_SIZE == 15


@pytest.mark.asyncio
async def test_mock_place_order_deterministic_id() -> None:
    client = MockPolymarketV2Client()
    order = V2Order(token_id="t1", side="BUY", price=Decimal("0.55"), size=Decimal("10"),
                    timestamp_ms=1700000000000)
    receipt = await client.place_order(order)
    assert receipt.accepted
    assert receipt.order_id.startswith("ord_")
    receipt2 = await client.place_order(order)
    assert receipt2.order_id == receipt.order_id


@pytest.mark.asyncio
async def test_mock_batch_limit() -> None:
    client = MockPolymarketV2Client()
    orders = [V2Order(token_id=f"t{i}", side="BUY", price=Decimal("0.5"), size=Decimal("1"),
                      timestamp_ms=i) for i in range(16)]
    with pytest.raises(ValueError, match="batch order limit"):
        await client.place_batch(orders)


@pytest.mark.asyncio
async def test_mock_heartbeat_runs_periodically() -> None:
    client = MockPolymarketV2Client(heartbeat_interval_s=0.05)
    client.start_heartbeat()
    await asyncio.sleep(0.25)
    await client.stop_heartbeat()
    assert client.heartbeat_count >= 3


@pytest.mark.asyncio
async def test_mock_cancel_all_returns_count() -> None:
    client = MockPolymarketV2Client()
    for i in range(3):
        await client.place_order(
            V2Order(token_id=f"t{i}", side="BUY", price=Decimal("0.5"),
                    size=Decimal("1"), timestamp_ms=i)
        )
    n = await client.cancel_all()
    assert n == 3
    assert await client.cancel_all() == 0
