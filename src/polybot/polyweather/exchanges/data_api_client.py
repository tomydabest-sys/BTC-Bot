"""Polymarket Data API — wallet positions + activity feed (read-only).

``data-api.polymarket.com`` exposes per-wallet on-chain history with no
auth. Two feeds matter for PolyWeather:

  /positions?user=<addr>   current holdings (size, avg/cur price, PnL)
  /activity?user=<addr>    chronological events (TRADE/REDEEM/SPLIT/MERGE…)

Both return a **bare JSON list** (not ``{"positions": [...]}``). Field names
are camelCase: ``asset`` (the CLOB token id), ``conditionId`` (the market),
``avgPrice``/``curPrice``, ``usdcSize``, etc. The earlier stub guessed
``{"positions": [...]}`` with ``tokenId``/``marketId``/``currentPrice`` and
parsed nothing against the live API; this matches the real shape verified
against live wallets.

The ``weather_only`` helpers reuse the Gamma temperature regexes so "what
counts as a weather market" has a single definition across the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog

from polybot.polyweather.exchanges.gamma_client import _TEMP_SLUG_RE, _TEMP_TITLE_RE

logger = structlog.get_logger()

DATA_API_BASE = "https://data-api.polymarket.com"


def is_weather_market(title: str | None, slug: str | None, event_slug: str | None) -> bool:
    """True if a position/activity record is a daily city-temperature market.

    Uses the same regexes as ``GammaClient._is_weather_event`` so the wallet
    feeds and the market scanner agree on what "weather" means. Activity
    titles read "Will the highest temperature in Miami be 84-85°F on May 29?"
    (matches ``temperature in``); slugs read "highest-temperature-in-miami…".
    """
    if _TEMP_TITLE_RE.search(title or ""):
        return True
    for s in (slug, event_slug):
        if _TEMP_SLUG_RE.search(s or ""):
            return True
    return False


def _dec(x: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(x))
    except (TypeError, ValueError, InvalidOperation):
        return Decimal(default)


def _int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


@dataclass
class WalletPosition:
    """A current holding from /positions."""

    asset: str            # CLOB token id
    condition_id: str     # market (conditionId)
    outcome: str          # "Yes" / "No"
    size: Decimal
    avg_price: Decimal
    current_price: Decimal
    current_value: Decimal
    cash_pnl: Decimal
    realized_pnl: Decimal
    title: str
    slug: str
    event_slug: str

    @property
    def is_weather(self) -> bool:
        return is_weather_market(self.title, self.slug, self.event_slug)


@dataclass
class WalletActivity:
    """One event from /activity (a trade, redemption, split, merge…)."""

    timestamp: int
    type: str             # TRADE / REDEEM / SPLIT / MERGE / MAKER_REBATE / …
    side: str             # BUY / SELL (empty for non-trades)
    asset: str
    condition_id: str
    outcome: str
    price: Decimal
    size: Decimal
    usdc_size: Decimal
    title: str
    slug: str
    event_slug: str
    tx_hash: str

    @property
    def is_weather(self) -> bool:
        return is_weather_market(self.title, self.slug, self.event_slug)


def _parse_position(raw: dict[str, Any]) -> WalletPosition | None:
    asset = str(raw.get("asset") or "")
    if not asset:
        return None
    return WalletPosition(
        asset=asset,
        condition_id=str(raw.get("conditionId") or ""),
        outcome=str(raw.get("outcome") or ""),
        size=_dec(raw.get("size")),
        avg_price=_dec(raw.get("avgPrice")),
        current_price=_dec(raw.get("curPrice")),
        current_value=_dec(raw.get("currentValue")),
        cash_pnl=_dec(raw.get("cashPnl")),
        realized_pnl=_dec(raw.get("realizedPnl")),
        title=str(raw.get("title") or ""),
        slug=str(raw.get("slug") or ""),
        event_slug=str(raw.get("eventSlug") or ""),
    )


def _parse_activity(raw: dict[str, Any]) -> WalletActivity | None:
    if not raw.get("conditionId") and not raw.get("asset"):
        return None
    return WalletActivity(
        timestamp=_int(raw.get("timestamp")),
        type=str(raw.get("type") or ""),
        side=str(raw.get("side") or "").upper(),
        asset=str(raw.get("asset") or ""),
        condition_id=str(raw.get("conditionId") or ""),
        outcome=str(raw.get("outcome") or ""),
        price=_dec(raw.get("price")),
        size=_dec(raw.get("size")),
        usdc_size=_dec(raw.get("usdcSize")),
        title=str(raw.get("title") or ""),
        slug=str(raw.get("slug") or ""),
        event_slug=str(raw.get("eventSlug") or ""),
        tx_hash=str(raw.get("transactionHash") or ""),
    )


class DataApiClient:
    """Read-only client for one wallet's position + activity history."""

    def __init__(
        self,
        wallet_address: str,
        base_url: str = DATA_API_BASE,
        timeout_s: float = 10.0,
    ) -> None:
        self._wallet = wallet_address
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s

    async def _get_list(self, path: str, params: dict[str, Any]) -> list[dict]:
        async with httpx.AsyncClient(
            timeout=self._timeout, headers={"User-Agent": "polyweather/1.0"}
        ) as client:
            try:
                resp = await client.get(f"{self._base}{path}", params=params)
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("data_api_fetch_failed", path=path, error=str(exc))
                return []
        # Live shape is a bare list; tolerate a {"<key>": [...]} wrapper too.
        if isinstance(payload, dict):
            payload = payload.get("positions") or payload.get("activity") or payload.get("data") or []
        if not isinstance(payload, list):
            return []
        return [p for p in payload if isinstance(p, dict)]

    async def positions(self, limit: int = 500) -> list[WalletPosition]:
        raw = await self._get_list("/positions", {"user": self._wallet, "limit": limit})
        out = [_parse_position(p) for p in raw]
        return [p for p in out if p is not None]

    async def activity(self, limit: int = 100) -> list[WalletActivity]:
        raw = await self._get_list("/activity", {"user": self._wallet, "limit": limit})
        out = [_parse_activity(a) for a in raw]
        return [a for a in out if a is not None]

    async def weather_activity(self, limit: int = 100) -> list[WalletActivity]:
        """Recent activity filtered to daily city-temperature markets."""
        return [a for a in await self.activity(limit=limit) if a.is_weather]


class MockDataApiClient:
    """Deterministic mock for tests — seed with explicit records."""

    def __init__(
        self,
        positions: list[WalletPosition] | None = None,
        activity: list[WalletActivity] | None = None,
    ) -> None:
        self._positions = list(positions or [])
        self._activity = list(activity or [])

    async def positions(self, limit: int = 500) -> list[WalletPosition]:
        return list(self._positions[:limit])

    async def activity(self, limit: int = 100) -> list[WalletActivity]:
        return list(self._activity[:limit])

    async def weather_activity(self, limit: int = 100) -> list[WalletActivity]:
        return [a for a in await self.activity(limit=limit) if a.is_weather]
