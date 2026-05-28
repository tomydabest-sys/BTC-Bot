"""Polymarket CLOB REST client (read-only).

Provides the live market state the engine needs for true paper-trading
against real prices: orderbook depth, midpoint, best bid/ask, last trade.
All endpoints documented at https://docs.polymarket.com/.

No authentication required for read-only endpoints, so this works in
`--live-data` paper mode with zero credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

logger = structlog.get_logger()

CLOB_BASE = "https://clob.polymarket.com"


@dataclass
class CLOBPriceLevel:
    price: float
    size: float


@dataclass
class CLOBBook:
    """Top-N snapshot of a token's order book."""

    token_id: str
    bids: list[CLOBPriceLevel] = field(default_factory=list)
    asks: list[CLOBPriceLevel] = field(default_factory=list)
    midpoint: float = 0.5
    last_trade_price: float | None = None
    timestamp_ms: int = 0

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def spread_bps(self) -> float:
        if not self.bids or not self.asks:
            return 10000.0
        mid = (self.best_bid + self.best_ask) / 2.0
        if mid <= 0:
            return 10000.0
        return ((self.best_ask - self.best_bid) / mid) * 10000.0


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class CLOBClient:
    """Live CLOB read client. Defensive against schema changes."""

    def __init__(self, base_url: str = CLOB_BASE, timeout_s: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict | None = None) -> Any:
        try:
            resp = await client.get(f"{self._base}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("clob_fetch_failed", path=path, params=params, error=str(exc))
            return None

    async def fetch_book(self, token_id: str) -> CLOBBook | None:
        """Fetch the L2 orderbook for one token_id."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            raw = await self._get(client, "/book", {"token_id": token_id})
        if not raw or not isinstance(raw, dict):
            return None

        bids = [
            CLOBPriceLevel(_safe_float(level.get("price")), _safe_float(level.get("size")))
            for level in (raw.get("bids") or [])
            if isinstance(level, dict)
        ]
        asks = [
            CLOBPriceLevel(_safe_float(level.get("price")), _safe_float(level.get("size")))
            for level in (raw.get("asks") or [])
            if isinstance(level, dict)
        ]
        bids = [b for b in bids if 0 < b.price < 1 and b.size > 0]
        asks = [a for a in asks if 0 < a.price < 1 and a.size > 0]
        # Polymarket sometimes returns ascending bids; sort defensively.
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        mid = 0.5
        if bids and asks:
            mid = (bids[0].price + asks[0].price) / 2.0

        return CLOBBook(
            token_id=token_id,
            bids=bids,
            asks=asks,
            midpoint=mid,
            timestamp_ms=int(raw.get("timestamp", 0) or 0),
        )

    async def fetch_midpoint(self, token_id: str) -> float | None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            raw = await self._get(client, "/midpoint", {"token_id": token_id})
        if not raw or not isinstance(raw, dict):
            return None
        return _safe_float(raw.get("mid") or raw.get("midpoint"), default=0.0) or None

    async def fetch_last_trade_price(self, token_id: str) -> float | None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            raw = await self._get(client, "/last-trade-price", {"token_id": token_id})
        if not raw or not isinstance(raw, dict):
            return None
        return _safe_float(raw.get("price"), default=0.0) or None

    async def fetch_price(self, token_id: str, side: str) -> float | None:
        """Best price on the given side. `side` ∈ {"BUY","SELL"}."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            raw = await self._get(client, "/price", {"token_id": token_id, "side": side})
        if not raw or not isinstance(raw, dict):
            return None
        return _safe_float(raw.get("price"), default=0.0) or None


class MockCLOBClient:
    """Returns deterministic books for tests; mirrors live-shape interface."""

    def __init__(self, default_mid: float = 0.5) -> None:
        self._default_mid = default_mid

    async def fetch_book(self, token_id: str) -> CLOBBook:
        return CLOBBook(
            token_id=token_id,
            bids=[CLOBPriceLevel(self._default_mid - 0.02, 100.0),
                  CLOBPriceLevel(self._default_mid - 0.04, 200.0)],
            asks=[CLOBPriceLevel(self._default_mid + 0.02, 100.0),
                  CLOBPriceLevel(self._default_mid + 0.04, 200.0)],
            midpoint=self._default_mid,
        )

    async def fetch_midpoint(self, token_id: str) -> float:
        return self._default_mid

    async def fetch_last_trade_price(self, token_id: str) -> float:
        return self._default_mid

    async def fetch_price(self, token_id: str, side: str) -> float:
        return self._default_mid + (0.02 if side.upper() == "SELL" else -0.02)
