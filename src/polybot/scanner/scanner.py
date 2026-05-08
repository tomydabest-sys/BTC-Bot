"""Market discovery and filtering — BTC Up/Down markets, all timeframes.

PATCHED: warns once at startup if resolution_window_days[0] > 0 while short
timeframes (5m/15m) are enabled — a config combination that silently filters
out all short-window markets.
"""

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

BTC_UPDOWN_PATTERN = re.compile(
    r"bitcoin\s+up\s+or\s+down", re.IGNORECASE
)

TIME_WINDOW_PATTERN = re.compile(
    r"(\d{1,2}):(\d{2})\s*(AM|PM)\s*-\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)


def is_btc_updown_market(question: str) -> bool:
    return bool(BTC_UPDOWN_PATTERN.search(question))


def estimate_window_minutes(question: str) -> int | None:
    """Parse '12:30AM-12:45AM' style time-range to compute window length."""
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

    if end <= start:
        end += 24 * 60

    return end - start


def classify_btc_market(question: str) -> str | None:
    """Classify a Bitcoin Up/Down market into a timeframe code."""
    if not is_btc_updown_market(question):
        return None

    q_lower = question.lower()

    if "4 hour" in q_lower or "4-hour" in q_lower or "4h" in q_lower:
        return "4h"
    if "1 hour" in q_lower or "1-hour" in q_lower or "hourly" in q_lower:
        return "1h"

    window = estimate_window_minutes(question)
    if window is not None:
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

    if "today" in q_lower or "tomorrow" in q_lower:
        return "daily"

    return "daily"


_TIMEFRAME_ALIASES: dict[str, str] = {
    "5 min": "5m", "5m": "5m", "5min": "5m",
    "15 min": "15m", "15m": "15m", "15min": "15m",
    "1 hour": "1h", "1h": "1h", "1hr": "1h", "60m": "1h", "hourly": "1h",
    "4 hour": "4h", "4h": "4h", "4hr": "4h", "240m": "4h",
    "daily": "daily", "1d": "daily", "1day": "daily", "day": "daily",
}


def normalize_timeframe(tf: str) -> str:
    """Map any timeframe label to canonical code (5m, 15m, 1h, 4h, daily)."""
    return _TIMEFRAME_ALIASES.get(tf.lower().strip(), tf.lower().strip())


_SHORT_TIMEFRAMES = {"5m", "15m"}


class MarketScanner:
    """Periodically discovers and filters BTC Up/Down markets across all timeframes."""

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
        self._market_timeframes: dict[str, str] = {}
        self._running = False
        self._validate_config()

    def _validate_config(self) -> None:
        """Warn loudly about config combinations that silently filter everything."""
        if not self._config.resolution_window_days:
            return
        min_days = self._config.resolution_window_days[0] if self._config.resolution_window_days else 0
        allowed = self._get_allowed_timeframes()
        if min_days > 0 and allowed and allowed & _SHORT_TIMEFRAMES:
            logger.warning(
                "scanner_config_warning",
                msg=(
                    f"resolution_window_days[0]={min_days} but timeframes include "
                    f"5m/15m. Short-window markets will ALL be filtered out."
                ),
                min_days=min_days,
                short_timeframes_enabled=sorted(allowed & _SHORT_TIMEFRAMES),
            )

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._scan_loop())

    async def stop(self) -> None:
        self._running = False

    @property
    def active_markets(self) -> dict[str, Market]:
        return dict(self._active_markets)

    def get_timeframe(self, market_id: str) -> str | None:
        return self._market_timeframes.get(market_id)

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                await self._scan()
            except Exception as e:
                logger.error("scan_error", error=str(e))
            await asyncio.sleep(self._config.interval_seconds)

    async def _scan(self) -> None:
        logger.info("scanning_btc_updown_markets",
                    timeframes=self._config.btc_timeframes)
        all_markets = await self._client.get_markets(active=True)

        btc_markets = []
        allowed_timeframes = self._get_allowed_timeframes()

        for market in all_markets:
            timeframe = classify_btc_market(market.question)
            if timeframe is None:
                continue

            if not self._passes_quality_filters(market):
                continue

            if allowed_timeframes and timeframe not in allowed_timeframes:
                continue

            btc_markets.append(market)
            self._market_timeframes[market.id] = timeframe

        btc_markets.sort(key=lambda m: m.volume_24h, reverse=True)

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
        if not market.active:
            return False

        if market.volume_24h < self._config.min_volume_24h:
            return False

        if market.liquidity < self._config.min_liquidity:
            return False

        now = datetime.utcnow().replace(tzinfo=None)
        min_days, max_days = self._config.resolution_window_days

        end_date = market.end_date.replace(tzinfo=None) if market.end_date.tzinfo else market.end_date

        if end_date < now + timedelta(days=min_days):
            return False
        if end_date > now + timedelta(days=max_days):
            return False

        return True

    def _get_allowed_timeframes(self) -> set[str] | None:
        raw = getattr(self._config, "btc_timeframes", None)
        if not raw:
            return None
        return {normalize_timeframe(t) for t in raw}
