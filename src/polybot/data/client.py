"""Polymarket API client — slug-based discovery for BTC Up/Down markets.

The Gamma API's generic /markets and /events endpoints don't properly filter
or sort, returning old/irrelevant markets. However, BTC up/down markets have
predictable slugs based on Unix timestamps:

    btc-updown-5m-{unix_ts}    (every 300 seconds)
    btc-updown-15m-{unix_ts}   (every 900 seconds)
    btc-updown-1h-{unix_ts}    (every 3600 seconds)
    btc-updown-4h-{unix_ts}    (every 14400 seconds)

This client fetches markets directly by constructing these slugs.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

import httpx
import structlog

from polybot.data.models import Market, OrderBook, PriceLevel, Trade, Side

logger = structlog.get_logger()

CLOB_BASE_URL = "https://clob.polymarket.com"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"

# Slug patterns for BTC up/down markets
# Format: (slug_prefix, interval_seconds, lookback_count, lookahead_count)
BTC_UPDOWN_WINDOWS = [
    ("btc-updown-5m",  300,   3, 2),   # 5-min:  check 3 past + 2 future
    ("btc-updown-15m", 900,   2, 2),   # 15-min: check 2 past + 2 future
    ("btc-updown-1h",  3600,  1, 1),   # 1-hour: check 1 past + 1 future
    ("btc-updown-4h",  14400, 1, 1),   # 4-hour: check 1 past + 1 future
]


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, max_requests: int = 10, per_seconds: float = 1.0) -> None:
        self._max = max_requests
        self._per = per_seconds
        self._tokens = float(max_requests)
        self._last_refill = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._last_refill == 0:
                self._last_refill = now
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
    """Async client — Gamma API for BTC up/down discovery, CLOB for trading."""

    def __init__(self, api_key: str, private_key: str = "") -> None:
        self._api_key = api_key
        self._private_key = private_key
        self._rate_limiter = RateLimiter(max_requests=10, per_seconds=1.0)
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

    async def get_balance(self) -> float:
        """Get wallet USDC balance. Returns 0 if not available."""
        # TODO: Implement via py-clob-client or web3
        return 0.0

    # ═══════════════════════════════════════════════════════════════
    #  MARKET DISCOVERY — slug-based lookup via Gamma /events
    # ═══════════════════════════════════════════════════════════════

    async def get_markets(self, active: bool = True, limit: int = 100) -> list[Market]:
        """Fetch current BTC up/down markets by constructing time-based slugs.

        Instead of paginating through thousands of irrelevant events,
        we build the predictable slugs for current/recent time windows
        and fetch each one directly.
        """
        assert self._gamma_client is not None, "Client not started"

        now = int(time.time())
        all_markets: list[Market] = []
        seen_ids: set[str] = set()

        for prefix, interval, lookback, lookahead in BTC_UPDOWN_WINDOWS:
            # Align to the interval boundary
            current_window = (now // interval) * interval

            # Check past, current, and future windows
            for offset in range(-lookback, lookahead + 1):
                ts = current_window + (offset * interval)
                slug = f"{prefix}-{ts}"

                try:
                    await self._rate_limiter.acquire()
                    resp = await self._gamma_client.get(
                        "/events",
                        params={"slug": slug},
                    )

                    if resp.status_code != 200:
                        continue

                    data = resp.json()

                    # Response can be a list or a single event dict
                    events = data if isinstance(data, list) else [data] if isinstance(data, dict) and data.get("title") else []

                    for event in events:
                        event_markets = event.get("markets", [])
                        for item in event_markets:
                            # Only include active, non-closed BTC markets
                            if not item.get("active", False):
                                continue
                            if item.get("closed", False):
                                continue

                            question = item.get("question", "")
                            if "bitcoin" not in question.lower():
                                continue

                            condition_id = item.get("conditionId", "")
                            if not condition_id or condition_id in seen_ids:
                                continue

                            seen_ids.add(condition_id)
                            market = self._parse_gamma_market(item)
                            if market:
                                all_markets.append(market)

                except httpx.HTTPStatusError:
                    continue
                except Exception as e:
                    logger.debug("slug_fetch_error", slug=slug, error=str(e))
                    continue

        logger.info(
            "markets_fetched",
            parsed_count=len(all_markets),
            slugs_checked=sum(
                lookback + lookahead + 1 for _, _, lookback, lookahead in BTC_UPDOWN_WINDOWS
            ),
        )
        return all_markets

    def _parse_gamma_market(self, item: dict) -> Market | None:
        """Parse a market from a Gamma API event's market object."""
        condition_id = item.get("conditionId", item.get("condition_id", ""))
        question = item.get("question", "")

        if not condition_id or not question:
            return None

        # Parse token IDs
        token_ids = []
        clob_token_ids = item.get("clobTokenIds")
        if clob_token_ids:
            if isinstance(clob_token_ids, str):
                try:
                    token_ids = json.loads(clob_token_ids)
                except (json.JSONDecodeError, TypeError):
                    token_ids = [clob_token_ids]
            elif isinstance(clob_token_ids, list):
                token_ids = clob_token_ids

        # Parse outcomes
        outcomes_raw = item.get("outcomes")
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except (json.JSONDecodeError, TypeError):
                outcomes = ["Up", "Down"]
        elif isinstance(outcomes_raw, list):
            outcomes = outcomes_raw
        else:
            outcomes = ["Up", "Down"]

        # Parse end date
        end_date_str = (
            item.get("endDate")
            or item.get("end_date_iso")
            or item.get("endDateIso")
            or ""
        )
        try:
            if end_date_str:
                end_date_str = end_date_str.replace("Z", "+00:00")
                end_date = datetime.fromisoformat(end_date_str)
            else:
                end_date = datetime.utcnow()
        except (ValueError, TypeError):
            end_date = datetime.utcnow()

        # Parse volume
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
            category="crypto",
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
                    outcome=item.get("outcome", "Up"),
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
