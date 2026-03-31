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

        # Fetch multiple pages to get more markets
        all_markets: list[Market] = []
        for offset in range(0, 300, 100):
            batch = await self._client.get_markets(active=True, limit=100, offset=offset)
            all_markets.extend(batch)
            if len(batch) < 100:
                break

        logger.info("markets_raw_fetched", count=len(all_markets))

        filtered = []
        filter_reasons: dict[str, int] = {}
        for m in all_markets:
            passes, reason = self._passes_filters_with_reason(m)
            if passes:
                filtered.append(m)
            else:
                filter_reasons[reason] = filter_reasons.get(reason, 0) + 1

        filtered.sort(key=lambda m: m.volume_24h, reverse=True)

        if filter_reasons:
            logger.info("markets_filtered_out", reasons=filter_reasons)

        new_markets = {m.id: m for m in filtered}
        added = set(new_markets) - set(self._active_markets)
        removed = set(self._active_markets) - set(new_markets)

        for market_id in added:
            await self._event_bus.emit("market_discovered", market=new_markets[market_id])
        for market_id in removed:
            await self._event_bus.emit("market_removed", market_id=market_id)

        self._active_markets = new_markets
        logger.info("scan_complete", total=len(filtered), added=len(added), removed=len(removed))

    def _passes_filters_with_reason(self, market: Market) -> tuple[bool, str]:
        """Check if market passes all filters. Returns (passes, reason_if_not)."""
        if not market.active:
            return False, "inactive"

        if market.volume_24h < self._config.min_volume_24h:
            return False, "low_volume"

        if market.liquidity < self._config.min_liquidity:
            return False, "low_liquidity"

        # Resolution window filter — skip for markets without a meaningful end date
        now = datetime.utcnow()
        min_days, max_days = self._config.resolution_window_days

        # Markets that already expired
        if market.end_date < now:
            return False, "expired"

        # Only apply max_days filter if configured (> 0)
        if max_days > 0 and market.end_date > now + timedelta(days=max_days):
            return False, "too_far_out"

        # Category filters
        if self._config.categories_allowlist:
            if market.category not in self._config.categories_allowlist:
                return False, "category_not_allowed"
        if market.category in self._config.categories_blocklist:
            return False, "category_blocked"

        # Must have token IDs to be tradeable
        if not market.token_ids:
            return False, "no_token_ids"

        return True, ""

    def _passes_filters(self, market: Market) -> bool:
        passes, _ = self._passes_filters_with_reason(market)
        return passes
