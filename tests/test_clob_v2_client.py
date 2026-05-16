"""Tests for the ClobV2Client wrapper (no live SDK / no live network)."""

from __future__ import annotations

import pytest

from polybot.api.clob_v2_client import (
    ClobV2Client,
    V2OrderArgs,
    V2SDKNotInstalled,
)


def test_v2_order_args_rejects_v1_fields():
    for banned in ("feeRateBps", "nonce", "taker"):
        with pytest.raises(ValueError, match=banned):
            V2OrderArgs(
                token_id="t", price=0.5, size=10, side="BUY",
                extra={banned: 1},
            )


def test_v2_order_args_validates_side():
    with pytest.raises(ValueError):
        V2OrderArgs(token_id="t", price=0.5, size=10, side="LONG")


def test_v2_order_args_validates_price_range():
    with pytest.raises(ValueError):
        V2OrderArgs(token_id="t", price=0.0, size=10, side="BUY")
    with pytest.raises(ValueError):
        V2OrderArgs(token_id="t", price=1.0, size=10, side="SELL")


def test_v2_order_args_validates_size():
    with pytest.raises(ValueError):
        V2OrderArgs(token_id="t", price=0.5, size=0, side="BUY")


def test_v2_order_args_uppercases_inputs():
    a = V2OrderArgs(token_id="t", price=0.5, size=1, side="buy", order_type="gtc")
    assert a.side == "BUY"
    assert a.order_type == "GTC"


@pytest.mark.asyncio
async def test_ensure_sdk_raises_when_not_installed():
    """The SDK package isn't installed in the test environment, so any
    live-only method should fail loudly rather than silently no-op."""
    client = ClobV2Client(private_key=None)
    args = V2OrderArgs(token_id="t", price=0.5, size=1, side="BUY")
    with pytest.raises(V2SDKNotInstalled):
        await client.post_order(args)


@pytest.mark.asyncio
async def test_get_fee_rate_uses_cache_after_first_fetch(monkeypatch):
    """If the HTTP call fails, the cached value (or fallback) is returned
    and the cache TTL prevents re-hitting the endpoint within the window."""
    client = ClobV2Client(fee_cache_ttl_s=60.0)
    await client.start()

    call_count = {"n": 0}

    class _FakeResp:
        status_code = 200
        def raise_for_status(self) -> None: ...
        def json(self) -> dict: return {"feeRate": "0.071"}

    async def _fake_get(url, params=None):  # type: ignore[no-untyped-def]
        call_count["n"] += 1
        return _FakeResp()

    monkeypatch.setattr(client._http, "get", _fake_get)
    fee1 = await client.get_fee_rate("tok-abc")
    fee2 = await client.get_fee_rate("tok-abc")
    assert fee1 == pytest.approx(0.071)
    assert fee2 == pytest.approx(0.071)
    assert call_count["n"] == 1
    await client.close()


@pytest.mark.asyncio
async def test_get_fee_rate_fallback_on_error(monkeypatch):
    client = ClobV2Client()
    await client.start()

    async def _fake_get(url, params=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("network down")

    monkeypatch.setattr(client._http, "get", _fake_get)
    fee = await client.get_fee_rate("tok-xyz")
    # Falls back to the documented crypto theta when nothing is cached.
    assert fee == pytest.approx(0.072)
    await client.close()


def test_normalise_post_response_back_compat():
    norm = ClobV2Client._normalise_post_response(
        {"transactionId": "abc"}
    )
    assert norm["transactionID"] == "abc"
    assert norm["state"] == "STATE_NEW"


@pytest.mark.asyncio
async def test_post_orders_rejects_oversized_batch():
    client = ClobV2Client()
    big = [
        V2OrderArgs(token_id=f"t{i}", price=0.5, size=1, side="BUY")
        for i in range(20)
    ]
    with pytest.raises(ValueError):
        await client.post_orders(big)
