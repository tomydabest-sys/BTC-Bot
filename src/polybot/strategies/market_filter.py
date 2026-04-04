"""Shared utilities for strategy market classification.

Based on real Polymarket market titles (as of April 2026):
- "Bitcoin Up or Down - April 5, 12:30AM-12:45AM ET"  (15-min)
- "Bitcoin Up or Down - April 4, 6:10PM-6:15PM ET"    (5-min)
- "Bitcoin above $84,250 on April 4?"                  (daily above/below)
- "What price will Bitcoin hit in April?"               (monthly)
- "Bitcoin price range on April 4?"                     (daily range)
- "Will Bitcoin be above $90,000 by end of April?"      (monthly above)

The key insight: real titles use "Up or Down" with embedded time ranges,
not keywords like "5 min" or "15 min". We match on the actual patterns.
"""

from __future__ import annotations

import re
from enum import Enum


class CryptoMarketType(str, Enum):
    """Classification of crypto prediction market types."""
    UPDOWN_SHORT = "updown_short"     # 5-min, 15-min up/down
    UPDOWN_HOURLY = "updown_hourly"   # 1-hour, 4-hour up/down
    UPDOWN_DAILY = "updown_daily"     # daily up/down
    ABOVE_BELOW = "above_below"       # "above $X on date"
    HIT_PRICE = "hit_price"           # "what price will X hit"
    PRICE_RANGE = "price_range"       # "price range on date"
    OTHER_CRYPTO = "other_crypto"     # crypto-related but unclassified


# Crypto asset keywords
_CRYPTO_ASSETS = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|matic|polygon|doge|dogecoin)\b",
    re.IGNORECASE,
)

# Time-embedded patterns in real Polymarket titles
# e.g. "12:30AM-12:45AM" (15 min), "6:10PM-6:15PM" (5 min)
_TIME_RANGE_PATTERN = re.compile(
    r"\d{1,2}:\d{2}\s*[AP]M\s*-\s*\d{1,2}:\d{2}\s*[AP]M",
    re.IGNORECASE,
)


def classify_crypto_market(question: str) -> CryptoMarketType | None:
    """Classify a market question into a crypto market type.

    Returns None if the market is not crypto-related.
    """
    q = question.lower()

    # Must mention a crypto asset
    if not _CRYPTO_ASSETS.search(question):
        return None

    # "Up or Down" markets (the main short-window type)
    if "up or down" in q or "higher or lower" in q:
        # Check for embedded time range to determine window length
        time_match = _TIME_RANGE_PATTERN.search(question)
        if time_match:
            # Parse the time range to estimate window length
            return CryptoMarketType.UPDOWN_SHORT
        if any(w in q for w in ["hour", "1h", "4h", "1-hour", "4-hour"]):
            return CryptoMarketType.UPDOWN_HOURLY
        if any(w in q for w in ["daily", "today", "day"]):
            return CryptoMarketType.UPDOWN_DAILY
        # Default: if it says "up or down" with a date, it's a short window
        return CryptoMarketType.UPDOWN_SHORT

    # "Above $X" markets
    if "above" in q or "below" in q:
        return CryptoMarketType.ABOVE_BELOW

    # "What price will X hit" markets
    if "hit" in q and "price" in q:
        return CryptoMarketType.HIT_PRICE

    # "Price range" markets
    if "price range" in q or "range" in q:
        return CryptoMarketType.PRICE_RANGE

    # Generic crypto market
    return CryptoMarketType.OTHER_CRYPTO


def is_crypto_window_market(
    question: str,
    custom_keywords: list[str] | None = None,
) -> bool:
    """Check if a market question matches crypto short-window patterns.

    This is the primary filter used by exchange-feed strategies (latency_arb,
    momentum_lag, volatility_breakout, monte_carlo) to identify markets where
    exchange price movements directly affect the outcome.

    Args:
        question: The market question text.
        custom_keywords: Optional override keywords. If provided, these are
            used INSTEAD of the classifier. Supports regex patterns.

    Returns:
        True if the market is a crypto short-window market.
    """
    # If custom keywords are provided, use those (backwards compat)
    if custom_keywords is not None:
        q = question.lower()
        for kw in custom_keywords:
            try:
                if re.search(kw, q):
                    return True
            except re.error:
                if kw in q:
                    return True
        return False

    # Use the classifier
    market_type = classify_crypto_market(question)
    if market_type is None:
        return False

    # These types are directly influenced by exchange price movements
    return market_type in {
        CryptoMarketType.UPDOWN_SHORT,
        CryptoMarketType.UPDOWN_HOURLY,
        CryptoMarketType.UPDOWN_DAILY,
        CryptoMarketType.ABOVE_BELOW,
        CryptoMarketType.HIT_PRICE,
        CryptoMarketType.PRICE_RANGE,
        CryptoMarketType.OTHER_CRYPTO,
    }
