"""Synthetic GBM BTC price feed for environments where Binance is unreachable.

Use when Test-NetConnection stream.binance.com -p 9443 fails (Windows firewall,
corporate proxy, geo-block). Provides 20 Hz ticks with realistic vol so the
strategies' realized-vol-based fair-value math still works.

Wire-up: in launcher.py or main.py:

    from polybot.data.mock_btc_feed import MockBTCFeed

    if args.mock_btc_feed:
        mock = MockBTCFeed(exchange_feed.feeds["BTC"], spot=77700.0, sigma_annual=0.45)
        asyncio.create_task(mock.run())
        # Don't start the real exchange feed
    else:
        await exchange_feed.start()
"""

from __future__ import annotations

import asyncio
import math
import random
import time

import structlog

from polybot.data.exchange_feed import ExchangeTick, PriceFeedState

logger = structlog.get_logger()


class MockBTCFeed:
    """Synthetic GBM BTC tick generator.

    Generates ticks at 20 Hz with given annualized vol. Adds occasional
    "burst" events (5x normal vol for 2-5 seconds, every 30-90 seconds)
    so overshoot_reversion and boundary_decay get realistic trigger
    conditions during testing.
    """

    def __init__(
        self,
        feed: PriceFeedState,
        spot: float = 77700.0,
        sigma_annual: float = 0.45,
        tick_interval_s: float = 0.05,  # 20 Hz
        burst_probability: float = 0.005,  # ~ once per 100s
        burst_duration_s: tuple[float, float] = (2.0, 5.0),
        burst_vol_multiplier: float = 5.0,
    ) -> None:
        self._feed = feed
        self._price = float(spot)
        self._sigma_annual = float(sigma_annual)
        self._sigma_per_sec = sigma_annual / math.sqrt(365.25 * 86400)
        self._tick_interval = float(tick_interval_s)
        self._burst_prob = float(burst_probability)
        self._burst_duration = burst_duration_s
        self._burst_mult = float(burst_vol_multiplier)
        self._running = False
        self._tick_count = 0
        self._burst_until: float = 0.0

    async def run(self) -> None:
        self._running = True
        logger.info(
            "mock_btc_feed_started",
            spot=self._price,
            sigma_annual=self._sigma_annual,
            tick_hz=int(1 / self._tick_interval),
        )
        while self._running:
            now = time.time()
            in_burst = now < self._burst_until

            # Maybe start a new burst
            if not in_burst and random.random() < self._burst_prob:
                duration = random.uniform(*self._burst_duration)
                self._burst_until = now + duration
                in_burst = True
                logger.debug("mock_burst_start", duration_s=round(duration, 2))

            # GBM step
            sigma = self._sigma_per_sec * self._tick_interval ** 0.5
            if in_burst:
                sigma *= self._burst_mult
            self._price *= math.exp(sigma * random.gauss(0, 1))

            tick = ExchangeTick(
                symbol="BTC",
                price=self._price,
                timestamp=now,
                source="mock_gbm",
            )
            self._feed.add_tick(tick)
            self._tick_count += 1

            if self._tick_count % 1000 == 0:
                logger.info(
                    "mock_btc_feed_progress",
                    ticks=self._tick_count,
                    price=round(self._price, 2),
                )

            await asyncio.sleep(self._tick_interval)

    def stop(self) -> None:
        self._running = False
