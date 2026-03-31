"""Shared utilities for strategy market classification."""

from __future__ import annotations

import re

# Default keyword patterns for identifying crypto short-window markets.
# These match common Polymarket question formats. Users can override
# via the `market_keywords` strategy parameter.
DEFAULT_CRYPTO_WINDOW_KEYWORDS: list[str] = [
    # Price direction patterns
    "up or down",
    "higher or lower",
    "above or below",
    "increase or decrease",
    "rise or fall",
    "go up",
    "go down",
    # Time window patterns
    r"\d+\s*min",       # "5 min", "15min", "5 minutes"
    r"\d+\s*hour",      # "1 hour", "24 hours"
    # Asset + direction
    "btc.*price",
    "eth.*price",
    "sol.*price",
    "xrp.*price",
    "bitcoin.*price",
    "ethereum.*price",
    # Polymarket specific formats
    "price of",
    "will.*be above",
    "will.*be below",
    "close above",
    "close below",
    "end above",
    "end below",
]


def is_crypto_window_market(
    question: str,
    custom_keywords: list[str] | None = None,
) -> bool:
    """Check if a market question matches crypto short-window patterns.

    Args:
        question: The market question text.
        custom_keywords: Optional override keywords. If provided, these are
            used INSTEAD of the defaults. Supports regex patterns.

    Returns:
        True if the question matches any keyword pattern.
    """
    keywords = custom_keywords if custom_keywords is not None else DEFAULT_CRYPTO_WINDOW_KEYWORDS
    q = question.lower()
    for kw in keywords:
        try:
            if re.search(kw, q):
                return True
        except re.error:
            # Fallback to simple substring match if regex is invalid
            if kw in q:
                return True
    return False
