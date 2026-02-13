"""Market discovery and filtering."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import structlog

from polybot.config import ScannerConfig
from polybot.data.client import PolymarketClient
from polybot.data.models import Market
from polybot.events import EventBus

logger = structlog.get_logger()


class MarketScanner:
    """Periodically discovers and filters tradeable markets."""

    def __init__(
        self,
        client: PolymarketClient,
        config: ScannerConfig,
        event_bus: EventBus,
    ) -> None:
        self._client = client
        self._config = config
        self._event_bus = event_bus
        self._active_markets: dict[str, Market] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._scan_loop())

    async def stop(self) -> None:
        self._running = False

    @property
    def active_markets(self) -> dict[str, Market]:
        return dict(self._active_markets)

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                await self._scan()
            except Exception as e:
                logger.error("scan_error", error=str(e))
            await asyncio.sleep(self._config.interval_seconds)

    async def _scan(self) -> None:
        logger.info("scanning_markets")
        all_markets = await self._client.get_markets(active=True)
        filtered = [m for m in all_markets if self._passes_filters(m)]
        filtered.sort(key=lambda m: m.volume_24h, reverse=True)

        new_markets = {m.id: m for m in filtered}
        added = set(new_markets) - set(self._active_markets)
        removed = set(self._active_markets) - set(new_markets)

        for market_id in added:
            await self._event_bus.emit("market_discovered", market=new_markets[market_id])
        for market_id in removed:
            await self._event_bus.emit("market_removed", market_id=market_id)

        self._active_markets = new_markets
        logger.info("scan_complete", total=len(filtered), added=len(added), removed=len(removed))

    def _passes_filters(self, market: Market) -> bool:
        if not market.active:
            return False
        if market.volume_24h < self._config.min_volume_24h:
            return False
        if market.liquidity < self._config.min_liquidity:
            return False

        # Resolution window filter
        now = datetime.utcnow()
        min_days, max_days = self._config.resolution_window_days
        if market.end_date < now + timedelta(days=min_days):
            return False
        if market.end_date > now + timedelta(days=max_days):
            return False

        # Category filters
        if self._config.categories_allowlist:
            if market.category not in self._config.categories_allowlist:
                return False
        if market.category in self._config.categories_blocklist:
            return False

        return True
