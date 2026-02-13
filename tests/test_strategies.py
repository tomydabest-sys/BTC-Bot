"""Tests for trading strategies."""

import pytest

from polybot.strategies.mean_reversion import MeanReversionStrategy
from polybot.strategies.momentum import MomentumStrategy
from polybot.data.models import Direction


@pytest.mark.asyncio
async def test_mean_reversion_no_signal_within_threshold(sample_snapshot):
    """No signal when price is close to fair value."""
    strategy = MeanReversionStrategy(deviation_threshold=0.10)  # High threshold
    signal = await strategy.evaluate(sample_snapshot)
    assert signal is None


@pytest.mark.asyncio
async def test_mean_reversion_generates_signal(sample_snapshot):
    """Signal generated when price deviates from fair value."""
    # Set VWAP far from mid price to trigger signal
    sample_snapshot.vwap_1h = 0.40  # Far below mid_price of 0.56
    strategy = MeanReversionStrategy(deviation_threshold=0.03)
    signal = await strategy.evaluate(sample_snapshot)
    assert signal is not None
    assert signal.direction == Direction.SELL  # Price above fair value
    assert signal.confidence > 0


@pytest.mark.asyncio
async def test_momentum_no_signal_insufficient_data(sample_snapshot):
    """No signal when price history is too short."""
    sample_snapshot.price_history = [0.5, 0.5]  # Only 2 points
    strategy = MomentumStrategy(price_change_threshold=0.05)
    signal = await strategy.evaluate(sample_snapshot)
    assert signal is None


@pytest.mark.asyncio
async def test_momentum_generates_signal_on_strong_move(sample_snapshot):
    """Signal generated on strong price movement."""
    # Create strong upward movement
    sample_snapshot.price_history = [0.40 + i * 0.005 for i in range(20)]
    strategy = MomentumStrategy(price_change_threshold=0.05)
    signal = await strategy.evaluate(sample_snapshot)
    assert signal is not None
    assert signal.direction == Direction.BUY
