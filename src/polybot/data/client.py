"""Polymarket API client — uses Gamma API for market discovery, CLOB for trading."""

from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
import structlog

from polybot.data.models import Market, OrderBook, PriceLevel, Trade, Side

logger = structlog.get_logger()

CLOB_BASE_URL = "https://clob.polymarket.com"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"


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
    """Async HTTP client for Polymarket — Gamma API for discovery, CLOB for trading."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._rate_limiter = RateLimiter(max_requests=5, per_seconds=1.0)
        self._clob_client: httpx.AsyncClient | None = None
        self._gamma_client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._clob_client = httpx.AsyncClient(
            base_url=CLOB_BASE_URL,
            headers=headers,
            timeout=30.0,
        )
        self._gamma_client = httpx.AsyncClient(
            base_url=GAMMA_BASE_URL,
            timeout=30.0,
        )

    async def close(self) -> None:
        if self._clob_client:
            await self._clob_client.aclose()
        if self._gamma_client:
            await self._gamma_client.aclose()

    # ═══════════════════════════════════════════════════════════════
    #  MARKET DISCOVERY — via Gamma API
    #
    #  The Gamma API returns currently active markets with proper
    #  filtering. The CLOB /markets endpoint returns from oldest
    #  first and is not suitable for discovery.
    # ═══════════════════════════════════════════════════════════════

    async def get_markets(self, active: bool = True, limit: int = 100) -> list[Market]:
        """Fetch active markets from the Gamma API.

        Uses the /markets endpoint on gamma-api.polymarket.com which
        returns currently active markets (not historical ones from 2023).
        """
        assert self._gamma_client is not None, "Client not started"
        await self._rate_limiter.acquire()

        all_markets: list[Market] = []
        offset = 0
        max_pages = 10  # Safety limit

        for _ in range(max_pages):
            try:
                response = await self._gamma_client.get(
                    "/markets",
                    params={
                        "active": "true" if active else "false",
                        "closed": "false",
                        "archived": "false",
                        "limit": limit,
                        "offset": offset,
                    },
                )
                response.raise_for_status()
                data = response.json()

                # Gamma API returns a list directly
                items = data if isinstance(data, list) else data.get("data", [])

                if not items:
                    break

                for item in items:
                    try:
                        market = self._parse_gamma_market(item)
                        if market:
                            all_markets.append(market)
                    except Exception as e:
                        logger.debug("market_parse_error", error=str(e))
                        continue

                # If we got fewer than limit, we've reached the end
                if len(items) < limit:
                    break

                offset += limit
                await self._rate_limiter.acquire()

            except httpx.HTTPStatusError as e:
                logger.error("gamma_api_error", status=e.response.status_code)
                break
            except Exception as e:
                logger.error("gamma_fetch_error", error=str(e))
                break

        logger.info("markets_fetched", parsed_count=len(all_markets), raw_offset=offset)
        return all_markets

    def _parse_gamma_market(self, item: dict) -> Market | None:
        """Parse a market from the Gamma API response."""
        # Gamma API field names differ slightly from CLOB
        condition_id = item.get("conditionId") or item.get("condition_id") or ""
        question = item.get("question") or ""

        if not condition_id or not question:
            return None

        # Parse token IDs from clobTokenIds or outcomes
        token_ids = []
        clob_token_ids = item.get("clobTokenIds")
        if clob_token_ids:
            if isinstance(clob_token_ids, str):
                # Sometimes it's a JSON string like "[\"id1\",\"id2\"]"
                import json
                try:
                    token_ids = json.loads(clob_token_ids)
                except (json.JSONDecodeError, TypeError):
                    token_ids = [clob_token_ids]
            elif isinstance(clob_token_ids, list):
                token_ids = clob_token_ids

        # Parse outcomes
        outcomes_raw = item.get("outcomes")
        if isinstance(outcomes_raw, str):
            import json
            try:
                outcomes = json.loads(outcomes_raw)
            except (json.JSONDecodeError, TypeError):
                outcomes = ["Yes", "No"]
        elif isinstance(outcomes_raw, list):
            outcomes = outcomes_raw
        else:
            outcomes = ["Yes", "No"]

        # Parse end date
        end_date_str = (
            item.get("endDate")
            or item.get("end_date_iso")
            or item.get("endDateIso")
            or ""
        )
        try:
            if end_date_str:
                # Handle various date formats
                end_date_str = end_date_str.replace("Z", "+00:00")
                end_date = datetime.fromisoformat(end_date_str)
            else:
                end_date = datetime.utcnow()
        except (ValueError, TypeError):
            end_date = datetime.utcnow()

        # Parse volume — Gamma uses different field names
        volume = 0.0
        for key in ["volume", "volumeNum", "volume_num", "volume24hr", "volume_num_24hr"]:
            val = item.get(key)
            if val is not None:
                try:
                    volume = float(val)
                    break
                except (ValueError, TypeError):
                    continue

        # Parse liquidity
        liquidity = 0.0
        for key in ["liquidity", "liquidityNum", "liquidity_num"]:
            val = item.get(key)
            if val is not None:
                try:
                    liquidity = float(val)
                    break
                except (ValueError, TypeError):
                    continue

        return Market(
            id=condition_id,
            question=question,
            slug=item.get("slug", item.get("market_slug", "")),
            outcomes=outcomes,
            token_ids=token_ids,
            end_date=end_date,
            category=item.get("category", item.get("groupItemTitle", "")),
            active=item.get("active", True),
            volume_24h=volume,
            liquidity=liquidity,
        )

    # ═══════════════════════════════════════════════════════════════
    #  TRADING — via CLOB API
    # ═══════════════════════════════════════════════════════════════

    async def _clob_request(self, method: str, path: str, **kwargs) -> dict:
        assert self._clob_client is not None, "Client not started"
        await self._rate_limiter.acquire()
        response = await self._clob_client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def get_orderbook(self, token_id: str) -> OrderBook:
        """Fetch current orderbook for a token from the CLOB."""
        data = await self._clob_request("GET", "/book", params={"token_id": token_id})
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
        """Fetch recent trades for a token from the CLOB."""
        data = await self._clob_request(
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
        return await self._clob_request("POST", "/order", json=order_payload)

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            await self._clob_request("DELETE", f"/order/{order_id}")
            return True
        except httpx.HTTPStatusError:
            logger.warning("cancel_order_failed", order_id=order_id)
            return False

    async def get_open_orders(self) -> list[dict]:
        """Fetch current open orders."""
        data = await self._clob_request("GET", "/orders")
        return data if isinstance(data, list) else data.get("data", [])
