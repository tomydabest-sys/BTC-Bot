"""Market discovery and filtering — BTC Up/Down markets only."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta

import structlog

from polybot.config import ScannerConfig
from polybot.data.client import PolymarketClient
from polybot.data.models import Market
from polybot.events import EventBus

logger = structlog.get_logger()

# ─── BTC Up/Down Detection ────────────────────────────────────────
# Polymarket BTC up/down market titles look like:
#   "Bitcoin Up or Down - April 5, 12:30AM-12:45AM ET"     (15-min)
#   "Bitcoin Up or Down - April 4, 6:10PM-6:15PM ET"       (5-min)
#   "Bitcoin Up or Down - April 4, 6:00PM-7:00PM ET"       (1-hour)
#   "Bitcoin Up or Down - April 4, 2:00PM-6:00PM ET"       (4-hour)
#
# We match on "bitcoin" + "up or down" in the question text.
# Everything else (politics, sports, price targets, etc.) is ignored.

BTC_UPDOWN_PATTERN = re.compile(
    r"bitcoin\s+up\s+or\s+down", re.IGNORECASE
)

# Pattern to extract the time window duration from the title
# Matches patterns like "12:30AM-12:45AM" or "6:00PM-7:00PM"
TIME_WINDOW_PATTERN = re.compile(
    r"(\d{1,2}):(\d{2})\s*(AM|PM)\s*-\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)


def is_btc_updown_market(question: str) -> bool:
    """Check if a market is a BTC Up/Down market."""
    return bool(BTC_UPDOWN_PATTERN.search(question))


def estimate_window_minutes(question: str) -> int | None:
    """Estimate the time window in minutes from the market question.

    Returns None if the window can't be determined.
    """
    match = TIME_WINDOW_PATTERN.search(question)
    if not match:
        return None

    h1, m1, ap1 = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    h2, m2, ap2 = int(match.group(4)), int(match.group(5)), match.group(6).upper()

    def to_minutes(h: int, m: int, ap: str) -> int:
        if ap == "PM" and h != 12:
            h += 12
        elif ap == "AM" and h == 12:
            h = 0
        return h * 60 + m

    start = to_minutes(h1, m1, ap1)
    end = to_minutes(h2, m2, ap2)

    # Handle midnight crossing (e.g., 11:45PM-12:00AM)
    if end <= start:
        end += 24 * 60

    return end - start


def classify_btc_market(question: str) -> str | None:
    """Classify a BTC market into a timeframe bucket.

    Returns: "5m", "15m", "1h", "4h", or None if not a BTC up/down market.
    """
    if not is_btc_updown_market(question):
        return None

    window = estimate_window_minutes(question)
    if window is None:
        return "unknown"

    if window <= 5:
        return "5m"
    elif window <= 15:
        return "15m"
    elif window <= 60:
        return "1h"
    elif window <= 240:
        return "4h"
    else:
        return "daily"


class MarketScanner:
    """Periodically discovers and filters BTC Up/Down markets only."""

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
        self._market_timeframes: dict[str, str] = {}  # market_id → "5m"/"15m"/etc
        self._running = False

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._scan_loop())

    async def stop(self) -> None:
        self._running = False

    @property
    def active_markets(self) -> dict[str, Market]:
        return dict(self._active_markets)

    def get_timeframe(self, market_id: str) -> str | None:
        """Get the classified timeframe for a market."""
        return self._market_timeframes.get(market_id)

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                await self._scan()
            except Exception as e:
                logger.error("scan_error", error=str(e))
            await asyncio.sleep(self._config.interval_seconds)

    async def _scan(self) -> None:
        logger.info("scanning_btc_updown_markets")
        # Fetch all active markets from Polymarket
        all_markets = await self._client.get_markets(active=True)

        # ═══ FILTER: Only BTC Up/Down markets ═══
        btc_markets = []
        for market in all_markets:
            timeframe = classify_btc_market(market.question)
            if timeframe is None:
                continue  # Not a BTC up/down market — skip entirely

            # Apply basic quality filters
            if not self._passes_quality_filters(market):
                continue

            # Optionally filter by specific timeframes
            allowed_timeframes = self._get_allowed_timeframes()
            if allowed_timeframes and timeframe not in allowed_timeframes:
                continue

            btc_markets.append(market)
            self._market_timeframes[market.id] = timeframe

        # Sort by volume (highest first)
        btc_markets.sort(key=lambda m: m.volume_24h, reverse=True)

        # Update active markets
        new_markets = {m.id: m for m in btc_markets}
        added = set(new_markets) - set(self._active_markets)
        removed = set(self._active_markets) - set(new_markets)

        for market_id in added:
            market = new_markets[market_id]
            tf = self._market_timeframes.get(market_id, "?")
            await self._event_bus.emit("market_discovered", market=market)
            logger.info(
                "btc_market_found",
                market_id=market.id,
                question=market.question[:60],
                timeframe=tf,
                volume=market.volume_24h,
            )

        for market_id in removed:
            await self._event_bus.emit("market_removed", market_id=market_id)
            self._market_timeframes.pop(market_id, None)

        self._active_markets = new_markets

        # Log summary by timeframe
        tf_counts: dict[str, int] = {}
        for mid in new_markets:
            tf = self._market_timeframes.get(mid, "?")
            tf_counts[tf] = tf_counts.get(tf, 0) + 1

        logger.info(
            "scan_complete",
            total_btc_updown=len(btc_markets),
            added=len(added),
            removed=len(removed),
            by_timeframe=tf_counts,
            total_scanned=len(all_markets),
            filtered_out=len(all_markets) - len(btc_markets),
        )

    def _passes_quality_filters(self, market: Market) -> bool:
        """Basic quality checks — volume, liquidity, spread, resolution window."""
        if not market.active:
            return False

        # Volume filter (relaxed — new windows start at $0)
        if market.volume_24h < self._config.min_volume_24h:
            return False

        # Liquidity filter
        if market.liquidity < self._config.min_liquidity:
            return False

        # Resolution window — only markets resolving soon
        now = datetime.utcnow()
        min_days, max_days = self._config.resolution_window_days
        if market.end_date < now + timedelta(days=min_days):
            return False
        if market.end_date > now + timedelta(days=max_days):
            return False

        return True

    def _get_allowed_timeframes(self) -> set[str] | None:
        """Get allowed timeframes from config, or None for all."""
        # Check if config has btc_timeframes (custom field)
        raw = getattr(self._config, "btc_timeframes", None)
        if not raw:
            return None

        mapping = {
            "5 min": "5m", "5m": "5m", "5min": "5m",
            "15 min": "15m", "15m": "15m", "15min": "15m",
            "1 hour": "1h", "1h": "1h", "1hr": "1h", "60m": "1h",
            "4 hour": "4h", "4h": "4h", "4hr": "4h", "240m": "4h",
            "daily": "daily", "1d": "daily",
        }
        return {mapping.get(t.lower().strip(), t.lower().strip()) for t in raw}
