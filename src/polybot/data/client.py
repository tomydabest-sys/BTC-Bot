"""Polymarket CLOB API client."""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import structlog

from polybot.data.models import Market, OrderBook, PriceLevel, Trade, Side

logger = structlog.get_logger()

CLOB_BASE_URL = "https://clob.polymarket.com"


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, max_requests: int = 5, per_seconds: float = 1.0) -> None:
        self._max = max_requests
        self._per = per_seconds
        self._tokens = float(max_requests)
        self._last_refill = asyncio.get_event_loop().time()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            elapsed = now - self._last_refill
            self._tokens = min(self._max, self._tokens + elapsed * (self._max / self._per))
            self._last_refill = now
            if self._tokens < 1:
                wait = (1 - self._tokens) * (self._per / self._max)
                await asyncio.sleep(wait)
                self._tokens = 0
            else:
                self._tokens -= 1


class PolymarketClient:
    """Async HTTP client for the Polymarket CLOB API."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._rate_limiter = RateLimiter(max_requests=5, per_seconds=1.0)
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=CLOB_BASE_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=30.0,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        assert self._client is not None, "Client not started"
        await self._rate_limiter.acquire()
        response = await self._client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def get_markets(
        self,
        active: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Market]:
        """Fetch active markets."""
        data = await self._request(
            "GET",
            "/markets",
            params={"active": active, "limit": limit, "offset": offset},
        )
        markets = []
        for item in data.get("data", data) if isinstance(data, dict) else data:
            markets.append(
                Market(
                    id=item["condition_id"],
                    question=item.get("question", ""),
                    slug=item.get("slug", ""),
                    outcomes=item.get("outcomes", ["Yes", "No"]),
                    token_ids=item.get("tokens", []),
                    end_date=datetime.fromisoformat(item["end_date_iso"])
                    if "end_date_iso" in item
                    else datetime.utcnow(),
                    category=item.get("category", ""),
                    active=item.get("active", True),
                    volume_24h=float(item.get("volume_num_24hr", 0)),
                    liquidity=float(item.get("liquidity_num", 0)),
                )
            )
        return markets

    async def get_orderbook(self, token_id: str) -> OrderBook:
        """Fetch current orderbook for a token."""
        data = await self._request("GET", "/book", params={"token_id": token_id})
        bids = [PriceLevel(float(b["price"]), float(b["size"])) for b in data.get("bids", [])]
        asks = [PriceLevel(float(a["price"]), float(a["size"])) for a in data.get("asks", [])]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        return OrderBook(
            market_id=token_id,
            timestamp=datetime.utcnow(),
            bids=bids,
            asks=asks,
        )

    async def get_trades(self, token_id: str, limit: int = 50) -> list[Trade]:
        """Fetch recent trades for a token."""
        data = await self._request(
            "GET", "/trades", params={"token_id": token_id, "limit": limit}
        )
        trades = []
        for item in data if isinstance(data, list) else data.get("data", []):
            trades.append(
                Trade(
                    market_id=token_id,
                    timestamp=datetime.fromisoformat(item["timestamp"])
                    if "timestamp" in item
                    else datetime.utcnow(),
                    side=Side(item.get("side", "BUY")),
                    price=float(item["price"]),
                    size=float(item["size"]),
                    outcome=item.get("outcome", "Yes"),
                )
            )
        return trades

    async def place_order(self, order_payload: dict) -> dict:
        """Submit a signed order to the CLOB."""
        return await self._request("POST", "/order", json=order_payload)

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            await self._request("DELETE", f"/order/{order_id}")
            return True
        except httpx.HTTPStatusError:
            logger.warning("cancel_order_failed", order_id=order_id)
            return False

    async def get_open_orders(self) -> list[dict]:
        """Fetch current open orders."""
        data = await self._request("GET", "/orders")
        return data if isinstance(data, list) else data.get("data", [])
