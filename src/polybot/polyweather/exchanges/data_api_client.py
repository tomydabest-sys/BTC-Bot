"""Polymarket Data API — wallet positions + MockClient."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import httpx


@dataclass
class WalletPosition:
    token_id: str
    market_id: str
    side: str
    size: Decimal
    avg_entry_price: Decimal
    current_price: Decimal


class DataApiClient:
    def __init__(self, wallet_address: str, base_url: str = "https://data-api.polymarket.com") -> None:
        self._wallet = wallet_address
        self._base = base_url.rstrip("/")

    async def positions(self) -> list[WalletPosition]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self._base}/positions", params={"user": self._wallet})
            resp.raise_for_status()
            data = resp.json()
        out: list[WalletPosition] = []
        for p in data.get("positions", []):
            out.append(
                WalletPosition(
                    token_id=p["tokenId"],
                    market_id=p["marketId"],
                    side=p.get("side", "BUY"),
                    size=Decimal(str(p["size"])),
                    avg_entry_price=Decimal(str(p["avgPrice"])),
                    current_price=Decimal(str(p.get("currentPrice", p["avgPrice"]))),
                )
            )
        return out


class MockDataApiClient:
    """Mock wallet — starts flat, the paper engine writes positions here."""

    def __init__(self) -> None:
        self._positions: list[WalletPosition] = []

    async def positions(self) -> list[WalletPosition]:
        return list(self._positions)

    def _set_positions(self, positions: list[WalletPosition]) -> None:
        self._positions = positions
