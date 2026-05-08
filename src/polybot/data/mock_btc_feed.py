"""Deterministic mock BTC feed for offline / testing use.

Use via:
    BTC_BOT_USE_MOCK_FEED=1 python -m polybot.dashboard.launcher --config ...

Or with --mock-btc-feed CLI flag (set by launcher.py).

Generates GBM-like ticks at 1Hz with configurable seed and parameters.
"""

from __future__ import annotations

import asyncio
import math
import random
import time

import structlog

logger = structlog.get_logger()


class MockBTCFeed:
    """Synthetic GBM tick generator. Pushes into an existing PriceFeedState."""

    def __init__(
        self,
        start_price: float = 95_000.0,
        annualized_vol: float = 0.45,
        drift: float = 0.0,
        tick_interval_s: float = 1.0,
        seed: int | None = None,
    ) -> None:
        self._start_price = float(start_price)
        self._sigma_annual = float(annualized_vol)
        self._mu_annual = float(drift)
        self._dt_s = float(tick_interval_s)
        self._rng = random.Random(seed if seed is not None else 0xBEEF)

    async def run(self, state) -> None:
        """Push ticks into a PriceFeedState until cancelled."""
        seconds_per_year = 365.25 * 86400
        sigma_per_step = self._sigma_annual * math.sqrt(self._dt_s / seconds_per_year)
        mu_per_step = self._mu_annual * (self._dt_s / seconds_per_year)
        price = self._start_price
        state.push(price)
        logger.info(
            "mock_btc_feed_started",
            start_price=price,
            sigma_annual=self._sigma_annual,
            tick_interval_s=self._dt_s,
        )
        try:
            while True:
                z = self._rng.gauss(0.0, 1.0)
                # Geometric Brownian motion step
                price *= math.exp((mu_per_step - 0.5 * sigma_per_step ** 2) + sigma_per_step * z)
                state.push(price, ts=time.time())
                await asyncio.sleep(self._dt_s)
        except asyncio.CancelledError:
            logger.info("mock_btc_feed_stopped", last_price=price)
            raise
