"""Wallet-watch poller — confirmation signal from tracked traders.

Polls ``data-api.polymarket.com`` activity for a configured set of wallets,
keeps only their *weather* (daily city-temperature) trades, and aggregates a
per-market "are the tracked wallets betting this bucket happens?" signal.

This is a **confirmation signal surfaced on the dashboard only** — it does
NOT gate or size trades. Treat it as a sanity check ("a wallet we track is
also long the Miami 84-85°F bucket"), never as a reason to loosen risk.

The watched-wallet list is operator-supplied (config/polyweather/wallets.yaml)
because the original weather-wallet analysis that named "the profitable four"
was not committed to the repo. scripts/polyweather/discover_weather_wallets.py
regenerates candidates from live data; the operator confirms which to trust.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import structlog

from polybot.polyweather.exchanges.data_api_client import (
    DataApiClient,
    WalletActivity,
)

logger = structlog.get_logger()

# A trade is "bullish on the bucket" (i.e. betting the temperature lands in
# that bucket) when it BUYs the Yes token or SELLs the No token.
def _bucket_direction(act: WalletActivity) -> int:
    side = act.side.upper()
    outcome = act.outcome.lower()
    if (side == "BUY" and outcome == "yes") or (side == "SELL" and outcome == "no"):
        return 1
    if (side == "BUY" and outcome == "no") or (side == "SELL" and outcome == "yes"):
        return -1
    return 0


@dataclass
class WalletSnapshot:
    """One tracked wallet's recent weather footprint."""

    address: str
    label: str
    weather_trades: int = 0
    last_trade_ts: int = 0
    net_usdc: Decimal = Decimal("0")   # signed bucket-bullish flow over the window
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "label": self.label,
            "weather_trades": self.weather_trades,
            "last_trade_ts": self.last_trade_ts,
            "net_usdc": self.net_usdc,
            "error": self.error,
        }


@dataclass
class MarketSignal:
    """Aggregated confirmation across tracked wallets for one market."""

    condition_id: str
    title: str
    event_slug: str
    wallets: int = 0          # distinct tracked wallets active in this market
    net_usdc: Decimal = Decimal("0")
    direction: str = "flat"   # "bullish" / "bearish" / "flat"
    last_trade_ts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "title": self.title,
            "event_slug": self.event_slug,
            "wallets": self.wallets,
            "net_usdc": self.net_usdc,
            "direction": self.direction,
            "last_trade_ts": self.last_trade_ts,
        }


@dataclass
class WalletWatchSnapshot:
    polled_at: float = 0.0
    lookback_seconds: float = 86400.0
    wallets: list[WalletSnapshot] = field(default_factory=list)
    market_signals: list[MarketSignal] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "polled_at": self.polled_at,
            "lookback_seconds": self.lookback_seconds,
            "wallet_count": len(self.wallets),
            "wallets": [w.to_dict() for w in self.wallets],
            "market_signals": [m.to_dict() for m in self.market_signals],
        }


ClientFactory = Callable[[str], Any]


class WalletWatcher:
    """Polls tracked wallets for recent weather activity.

    ``client_factory(address)`` returns an object exposing
    ``weather_activity(limit)`` — a live ``DataApiClient`` by default, or a
    ``MockDataApiClient`` in tests.
    """

    def __init__(
        self,
        wallets: list[dict[str, str]] | None = None,
        *,
        client_factory: ClientFactory | None = None,
        activity_limit: int = 200,
        lookback_seconds: float = 86400.0,
    ) -> None:
        self._wallets = [
            {"address": str(w["address"]).lower(), "label": str(w.get("label") or w["address"])}
            for w in (wallets or [])
            if w.get("address")
        ]
        self._client_factory: ClientFactory = client_factory or (lambda addr: DataApiClient(addr))
        self._activity_limit = activity_limit
        self._lookback_seconds = lookback_seconds

    @property
    def enabled(self) -> bool:
        return bool(self._wallets)

    async def poll(self, now: float | None = None) -> WalletWatchSnapshot:
        now = now if now is not None else time.time()
        cutoff = now - self._lookback_seconds
        snapshot = WalletWatchSnapshot(polled_at=now, lookback_seconds=self._lookback_seconds)
        # condition_id -> aggregation
        markets: dict[str, MarketSignal] = {}
        seen_wallets: dict[str, set[str]] = {}

        for w in self._wallets:
            addr, label = w["address"], w["label"]
            wsnap = WalletSnapshot(address=addr, label=label)
            try:
                client = self._client_factory(addr)
                acts = await _maybe_await(client.weather_activity(limit=self._activity_limit))
            except Exception as exc:  # noqa: BLE001
                wsnap.error = str(exc)
                logger.warning("wallet_watch_poll_failed", address=addr, error=str(exc))
                snapshot.wallets.append(wsnap)
                continue

            for act in acts:
                if act.timestamp and act.timestamp < cutoff:
                    continue
                if act.type and act.type.upper() != "TRADE":
                    continue
                direction = _bucket_direction(act)
                signed = act.usdc_size * direction
                wsnap.weather_trades += 1
                wsnap.last_trade_ts = max(wsnap.last_trade_ts, act.timestamp)
                wsnap.net_usdc += signed

                cid = act.condition_id or act.asset
                ms = markets.get(cid)
                if ms is None:
                    ms = MarketSignal(
                        condition_id=cid, title=act.title, event_slug=act.event_slug
                    )
                    markets[cid] = ms
                    seen_wallets[cid] = set()
                ms.net_usdc += signed
                ms.last_trade_ts = max(ms.last_trade_ts, act.timestamp)
                seen_wallets[cid].add(addr)

            snapshot.wallets.append(wsnap)

        for cid, ms in markets.items():
            ms.wallets = len(seen_wallets[cid])
            if ms.net_usdc > 0:
                ms.direction = "bullish"
            elif ms.net_usdc < 0:
                ms.direction = "bearish"
        # Strongest conviction (by absolute net flow) first.
        snapshot.market_signals = sorted(
            markets.values(), key=lambda m: abs(m.net_usdc), reverse=True
        )
        logger.info(
            "wallet_watch_polled",
            wallets=len(snapshot.wallets),
            markets=len(snapshot.market_signals),
        )
        return snapshot


async def _maybe_await(value: Awaitable[Any] | Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value
