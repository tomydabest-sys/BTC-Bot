"""Polymarket CLOB API client — wraps py-clob-client for auth + signing."""

from __future__ import annotations

import asyncio
from datetime import datetime
from functools import partial

import httpx
import structlog

from polybot.data.models import Market, OrderBook, PriceLevel, Side, Trade

logger = structlog.get_logger()

CLOB_BASE_URL = "https://clob.polymarket.com"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"


class RateLimiter:
    """Token bucket rate limiter."""

    def __init__(self, max_requests: int = 5, per_seconds: float = 1.0) -> None:
        self._max = max_requests
        self._per = per_seconds
        self._tokens = float(max_requests)
        self._last_refill = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self._last_refill == 0.0:
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
    """Async client for Polymarket CLOB — uses py-clob-client for signing."""

    def __init__(
        self,
        api_key: str,
        private_key: str = "",
        chain_id: int = 137,
    ) -> None:
        self._api_key = api_key
        self._private_key = private_key
        self._chain_id = chain_id
        self._rate_limiter = RateLimiter(max_requests=5, per_seconds=1.0)
        self._http: httpx.AsyncClient | None = None
        self._clob_client = None  # py_clob_client.ClobClient (sync, run in executor)

    async def start(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=CLOB_BASE_URL,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

        # Initialize py-clob-client only if credentials look valid
        # A valid private key is a 64-char hex string (with optional 0x prefix)
        pk = self._private_key.strip()
        pk_hex = pk[2:] if pk.startswith("0x") else pk
        has_valid_pk = bool(pk_hex) and all(c in "0123456789abcdefABCDEF" for c in pk_hex)

        if has_valid_pk and self._api_key:
            try:
                from py_clob_client.client import ClobClient

                self._clob_client = ClobClient(
                    CLOB_BASE_URL,
                    key=self._private_key,
                    chain_id=self._chain_id,
                )
                # Derive API creds from the CLOB client
                self._clob_client.set_api_creds(
                    self._clob_client.create_or_derive_api_creds()
                )
                logger.info("clob_client_initialized", chain_id=self._chain_id)
            except ImportError:
                logger.warning("py_clob_client_not_installed, live trading disabled")
                self._clob_client = None
            except Exception as e:
                logger.error("clob_client_init_error", error=str(e))
                self._clob_client = None
        elif pk and not has_valid_pk:
            logger.warning("private_key_not_hex", hint="Key must be a hex string (with optional 0x prefix). Paper mode will work without it.")

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict | list:
        assert self._http is not None, "Client not started — call start() first"
        await self._rate_limiter.acquire()
        response = await self._http.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    # ── Market Data (public, no signing needed) ────────────────────────

    async def get_markets(
        self,
        active: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Market]:
        """Fetch active markets from the Gamma API."""
        import json as _json

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{GAMMA_BASE_URL}/markets",
                params={
                    "active": str(active).lower(),
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                    "order": "volume24hr",
                    "ascending": "false",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        items = data if isinstance(data, list) else data.get("data", [])
        markets = []
        for item in items:
            # Parse token IDs — Gamma returns clobTokenIds as a JSON string
            token_ids = []
            raw_tokens = item.get("clobTokenIds")
            if isinstance(raw_tokens, str):
                try:
                    token_ids = _json.loads(raw_tokens)
                except (ValueError, TypeError):
                    token_ids = []
            elif isinstance(raw_tokens, list):
                token_ids = raw_tokens

            # Fallback to tokens field if clobTokenIds is missing
            if not token_ids:
                tokens = item.get("tokens", [])
                if isinstance(tokens, list):
                    for t in tokens:
                        if isinstance(t, dict):
                            token_ids.append(t.get("token_id", ""))
                        else:
                            token_ids.append(str(t))

            # Parse outcomes — may also be a JSON string
            raw_outcomes = item.get("outcomes")
            if isinstance(raw_outcomes, str):
                try:
                    outcomes = _json.loads(raw_outcomes)
                except (ValueError, TypeError):
                    outcomes = ["Yes", "No"]
            elif isinstance(raw_outcomes, list):
                outcomes = raw_outcomes
            else:
                outcomes = ["Yes", "No"]

            # Parse end date — try multiple field names
            end_date_raw = (
                item.get("end_date_iso")
                or item.get("endDate")
                or item.get("end_date")
                or ""
            )
            try:
                end_date = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00")) if end_date_raw else datetime.utcnow()
                # Strip timezone info so all comparisons use naive UTC
                if end_date.tzinfo is not None:
                    end_date = end_date.replace(tzinfo=None)
            except (ValueError, TypeError):
                end_date = datetime.utcnow()

            # Condition ID — try both camelCase and snake_case
            condition_id = (
                item.get("conditionId")
                or item.get("condition_id")
                or item.get("id", "")
            )

            # Volume/liquidity — try multiple field names
            volume_24h = float(
                item.get("volume24hr")
                or item.get("volume_num_24hr")
                or item.get("volume", 0)
            )
            liquidity = float(
                item.get("liquidity")
                or item.get("liquidity_num", 0)
            )

            if not condition_id:
                continue

            markets.append(
                Market(
                    id=condition_id,
                    question=item.get("question", ""),
                    slug=item.get("slug", item.get("market_slug", "")),
                    outcomes=outcomes,
                    token_ids=token_ids,
                    end_date=end_date,
                    category=item.get("category", ""),
                    active=item.get("active", True),
                    volume_24h=volume_24h,
                    liquidity=liquidity,
                )
            )

        logger.info(
            "markets_fetched",
            raw_count=len(items),
            parsed_count=len(markets),
        )
        return markets

    async def get_orderbook(self, token_id: str) -> OrderBook:
        """Fetch current orderbook for a token."""
        data = await self._request("GET", "/book", params={"token_id": token_id})
        if isinstance(data, list):
            data = {}
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
        items = data if isinstance(data, list) else data.get("data", [])
        trades = []
        for item in items:
            try:
                ts = datetime.fromisoformat(item["timestamp"]) if "timestamp" in item else datetime.utcnow()
            except (ValueError, TypeError):
                ts = datetime.utcnow()
            trades.append(
                Trade(
                    market_id=token_id,
                    timestamp=ts,
                    side=Side(item.get("side", "BUY")),
                    price=float(item["price"]),
                    size=float(item["size"]),
                    outcome=item.get("outcome", "Yes"),
                )
            )
        return trades

    # ── Wallet / Balance ───────────────────────────────────────────────

    async def get_balance(self) -> float:
        """Fetch USDC balance from the CLOB API."""
        if not self._clob_client:
            return 0.0
        try:
            loop = asyncio.get_running_loop()
            # py-clob-client is synchronous, run in executor
            result = await loop.run_in_executor(
                None, self._clob_client.get_balance_allowance
            )
            # Returns dict with 'balance' key (in Wei for USDC, 6 decimals)
            if isinstance(result, dict):
                raw = float(result.get("balance", 0))
                return raw / 1e6  # Convert from USDC wei to dollars
            return 0.0
        except Exception as e:
            logger.debug("balance_fetch_error", error=str(e))
            return 0.0

    # ── Order Signing & Placement ──────────────────────────────────────

    async def place_order(self, order_payload: dict) -> dict:
        """Build, sign, and submit an order via py-clob-client."""
        if not self._clob_client:
            raise RuntimeError(
                "Cannot place live orders: py-clob-client not initialized. "
                "Ensure POLYMARKET_PRIVATE_KEY and POLYMARKET_API_KEY are set."
            )

        from py_clob_client.order_builder.constants import BUY, SELL

        loop = asyncio.get_running_loop()

        # Build order args for py-clob-client
        side = BUY if order_payload["side"] == "BUY" else SELL
        token_id = order_payload["token_id"]
        price = float(order_payload["price"])
        size = float(order_payload["size"])

        # Create and sign the order (synchronous call)
        signed_order = await loop.run_in_executor(
            None,
            partial(
                self._clob_client.create_and_post_order,
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            ),
        )

        if isinstance(signed_order, dict):
            return signed_order

        # If it returned an object, try to extract useful fields
        return {"id": str(getattr(signed_order, "id", "")), "status": "submitted"}

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if self._clob_client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, partial(self._clob_client.cancel, order_id)
                )
                return True
            except Exception as e:
                logger.warning("cancel_order_failed", order_id=order_id, error=str(e))
                return False

        # Fallback to raw HTTP if no clob client
        try:
            await self._request("DELETE", f"/order/{order_id}")
            return True
        except httpx.HTTPStatusError:
            logger.warning("cancel_order_failed", order_id=order_id)
            return False

    async def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        if self._clob_client:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._clob_client.cancel_all)
                return True
            except Exception as e:
                logger.warning("cancel_all_failed", error=str(e))
                return False
        return False

    async def get_open_orders(self) -> list[dict]:
        """Fetch current open orders."""
        if self._clob_client:
            try:
                loop = asyncio.get_running_loop()
                orders = await loop.run_in_executor(
                    None, self._clob_client.get_orders
                )
                return orders if isinstance(orders, list) else []
            except Exception as e:
                logger.debug("get_orders_error", error=str(e))
                return []

        data = await self._request("GET", "/orders")
        return data if isinstance(data, list) else data.get("data", [])
