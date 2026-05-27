"""Polymarket V2 CLOB client wrapper + MockClient.

Live mode uses ``py-clob-client`` V2 (post-April-2026). EIP-712 domain
version MUST be the string ``"2"``, NOT int 2 — this is the #1 V2 migration
bug. Batch order size is 15. Fees are fetched dynamically per market via
``/fee-rate?token_id=...`` and never hardcoded.

The bot must run a 5s heartbeat or V2 cancels all resting orders, so this
module exposes a ``heartbeat`` coroutine intended to live in its own
asyncio.Task.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

logger = structlog.get_logger()

EIP712_DOMAIN_VERSION = "2"  # MUST be string. Do not change to int.
CTF_EXCHANGE_V2_CHAIN_ID = 137
DEFAULT_HEARTBEAT_INTERVAL_S = 5.0
BATCH_ORDER_SIZE = 15


@dataclass
class V2Order:
    """V2 order struct — note: no nonce, no feeRateBps, no taker."""

    token_id: str
    side: str  # "BUY" or "SELL"
    price: Decimal
    size: Decimal
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    metadata: dict[str, Any] = field(default_factory=dict)
    builder: str = ""


@dataclass
class V2OrderReceipt:
    order_id: str
    accepted: bool
    fill_price: Decimal | None = None
    filled_size: Decimal | None = None
    reason: str = ""


class _HeartbeatMixin:
    """Heartbeat task that any client (live or mock) can host."""

    def __init__(self, interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S) -> None:
        self.heartbeat_interval_s = float(interval_s)
        self.last_heartbeat_ts: float = 0.0
        self.heartbeat_count: int = 0
        self._hb_task: asyncio.Task | None = None
        self._hb_stop = asyncio.Event()

    async def _heartbeat_loop(self) -> None:
        while not self._hb_stop.is_set():
            try:
                await self._send_heartbeat()
                self.last_heartbeat_ts = time.time()
                self.heartbeat_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("heartbeat_failed", error=str(exc))
            try:
                await asyncio.wait_for(self._hb_stop.wait(), timeout=self.heartbeat_interval_s)
            except TimeoutError:
                continue

    async def _send_heartbeat(self) -> None:
        """Subclass override — mock does a no-op, live POSTs."""

    def start_heartbeat(self) -> asyncio.Task:
        if self._hb_task and not self._hb_task.done():
            return self._hb_task
        self._hb_stop.clear()
        self._hb_task = asyncio.create_task(self._heartbeat_loop(), name="polymarket_v2_heartbeat")
        return self._hb_task

    async def stop_heartbeat(self) -> None:
        self._hb_stop.set()
        if self._hb_task is not None:
            try:
                await asyncio.wait_for(self._hb_task, timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                self._hb_task.cancel()


class PolymarketV2Client(_HeartbeatMixin):
    """Thin wrapper over ``py-clob-client`` V2.

    Import is deferred so paper-mock mode works with zero credentials.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        proxy_wallet: str,
        chain_id: int = CTF_EXCHANGE_V2_CHAIN_ID,
        signature_type: int = 1,
        heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    ) -> None:
        super().__init__(heartbeat_interval_s)
        self._chain_id = chain_id
        self._signature_type = signature_type
        self._proxy_wallet = proxy_wallet
        try:
            from py_clob_client.client import ClobClient  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "py-clob-client (V2) is required for live mode. "
                "Install via `pip install -e .[live]` and ensure V2 release."
            ) from exc
        self._sdk = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=chain_id,
            key=api_secret,
            creds={
                "api_key": api_key,
                "api_secret": api_secret,
                "api_passphrase": api_passphrase,
            },
            signature_type=signature_type,
            funder=proxy_wallet,
        )
        self._fee_cache: dict[str, Decimal] = {}

    async def fetch_fee_rate_bps(self, token_id: str) -> Decimal:
        """Per-market dynamic fee. Never hardcode."""
        if token_id in self._fee_cache:
            return self._fee_cache[token_id]
        # Delegated to SDK (synchronous) — wrap in to_thread
        rate = await asyncio.to_thread(self._sdk.get_fee_rate, token_id)
        bps = Decimal(str(rate))
        self._fee_cache[token_id] = bps
        return bps

    async def place_order(self, order: V2Order) -> V2OrderReceipt:
        payload = {
            "tokenId": order.token_id,
            "side": order.side,
            "price": str(order.price),
            "size": str(order.size),
            "timestamp": order.timestamp_ms,
            "metadata": order.metadata,
            "builder": order.builder,
            "domainVersion": EIP712_DOMAIN_VERSION,
        }
        result = await asyncio.to_thread(self._sdk.post_order, payload)
        return V2OrderReceipt(
            order_id=str(result.get("orderId", "")),
            accepted=bool(result.get("success", False)),
            reason=str(result.get("errorMsg", "")),
        )

    async def place_batch(self, orders: list[V2Order]) -> list[V2OrderReceipt]:
        if len(orders) > BATCH_ORDER_SIZE:
            raise ValueError(f"V2 batch order limit is {BATCH_ORDER_SIZE}, got {len(orders)}")
        return [await self.place_order(o) for o in orders]

    async def cancel_all(self) -> int:
        return int(await asyncio.to_thread(self._sdk.cancel_all))

    async def _send_heartbeat(self) -> None:
        await asyncio.to_thread(self._sdk.ping)


class MockPolymarketV2Client(_HeartbeatMixin):
    """Deterministic in-memory exchange.

    Order IDs are seeded from ``(token_id, side, price, size)`` so the same
    inputs always produce the same ID. Heartbeat is a no-op but still ticks
    ``last_heartbeat_ts`` and ``heartbeat_count`` so the dashboard renders.
    """

    def __init__(self, heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S) -> None:
        super().__init__(heartbeat_interval_s)
        self._orders: dict[str, V2Order] = {}
        self._fee_bps = Decimal("0")  # maker = 0 on weather (per prompt §4 §0)

    @staticmethod
    def _make_order_id(order: V2Order) -> str:
        seed = f"{order.token_id}|{order.side}|{order.price}|{order.size}|{order.timestamp_ms}"
        return "ord_" + hashlib.sha256(seed.encode()).hexdigest()[:16]

    async def fetch_fee_rate_bps(self, token_id: str) -> Decimal:
        # Per the prompt: weather is maker-only, fees=0 + daily rebate
        return self._fee_bps

    async def place_order(self, order: V2Order) -> V2OrderReceipt:
        oid = self._make_order_id(order)
        self._orders[oid] = order
        # Maker simulation: always accepted, fill modeled by the paper engine
        return V2OrderReceipt(order_id=oid, accepted=True)

    async def place_batch(self, orders: list[V2Order]) -> list[V2OrderReceipt]:
        if len(orders) > BATCH_ORDER_SIZE:
            raise ValueError(f"V2 batch order limit is {BATCH_ORDER_SIZE}, got {len(orders)}")
        return [await self.place_order(o) for o in orders]

    async def cancel_all(self) -> int:
        n = len(self._orders)
        self._orders.clear()
        return n

    async def _send_heartbeat(self) -> None:
        return None
