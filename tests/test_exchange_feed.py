"""Tests for ExchangeFeed — REST source parsers + staleness watchdog state.

Binance is frequently geo-blocked from cloud/sandbox IPs, which silently
killed the BTC-dependent strategies (overshoot_reversion, boundary_decay)
and made the bot look halted. We now try Binance → Coinbase → Kraken on
the REST fallback path, surface feed-age in the dashboard, and warn when
the feed has been silent too long. These tests pin those guarantees.
"""

from __future__ import annotations

import time

import pytest

from polybot.data.exchange_feed import (
    REST_PRICE_SOURCES,
    STALENESS_WARN_S,
    ExchangeFeed,
    PriceFeedState,
    _parse_binance,
    _parse_coinbase,
    _parse_kraken,
)


class TestRestSourceParsers:
    def test_binance_parser(self):
        assert _parse_binance({"symbol": "BTCUSDT", "price": "82345.67"}) == 82345.67

    def test_binance_parser_missing_price(self):
        assert _parse_binance({"symbol": "BTCUSDT"}) == 0.0

    def test_binance_parser_empty(self):
        assert _parse_binance({}) == 0.0

    def test_coinbase_parser(self):
        body = {"data": {"base": "BTC", "currency": "USD", "amount": "82345.67"}}
        assert _parse_coinbase(body) == 82345.67

    def test_coinbase_parser_missing_data(self):
        assert _parse_coinbase({}) == 0.0

    def test_coinbase_parser_missing_amount(self):
        assert _parse_coinbase({"data": {}}) == 0.0

    def test_kraken_parser_full_key(self):
        body = {"result": {"XXBTZUSD": {"c": ["82345.67", "0.001"]}}}
        assert _parse_kraken(body) == 82345.67

    def test_kraken_parser_short_key(self):
        # Some Kraken endpoints return shorter pair codes
        body = {"result": {"XBTUSD": {"c": ["82345.67", "0.001"]}}}
        assert _parse_kraken(body) == 82345.67

    def test_kraken_parser_empty(self):
        assert _parse_kraken({}) == 0.0
        assert _parse_kraken({"result": {}}) == 0.0
        assert _parse_kraken({"result": {"XXBTZUSD": {}}}) == 0.0

    def test_kraken_parser_malformed_close(self):
        # Should not raise on missing `c` list
        body = {"result": {"XXBTZUSD": {"c": []}}}
        assert _parse_kraken(body) == 0.0


class TestRestSourceRegistry:
    def test_binance_listed_first(self):
        """Binance should remain the primary source. The alternates exist for
        fallback, not to replace the primary."""
        assert REST_PRICE_SOURCES[0][0] == "binance"

    def test_three_distinct_sources(self):
        names = [name for name, _, _ in REST_PRICE_SOURCES]
        assert sorted(names) == ["binance", "coinbase", "kraken"]

    def test_all_sources_have_https_urls(self):
        for _, url, _ in REST_PRICE_SOURCES:
            assert url.startswith("https://"), url


class TestFeedAgeAndStaleness:
    def test_feed_age_none_when_no_ticks(self):
        feed = ExchangeFeed(symbol="BTC")
        assert feed.feed_age_s is None
        assert feed.is_stale is False  # No ticks at all is not "stale"

    def test_feed_age_zero_after_fresh_tick(self):
        feed = ExchangeFeed(symbol="BTC")
        feed.state.push(82000.0)
        age = feed.feed_age_s
        assert age is not None
        assert age < 1.0

    def test_is_stale_after_threshold(self):
        feed = ExchangeFeed(symbol="BTC")
        # Push a tick with a stale timestamp
        feed.state.push(82000.0, ts=time.time() - (STALENESS_WARN_S + 5))
        assert feed.is_stale is True

    def test_is_fresh_below_threshold(self):
        feed = ExchangeFeed(symbol="BTC")
        feed.state.push(82000.0, ts=time.time() - (STALENESS_WARN_S / 2))
        assert feed.is_stale is False

    def test_last_source_starts_empty(self):
        feed = ExchangeFeed(symbol="BTC")
        assert feed.last_source == ""


class TestPriceFeedStatePush:
    def test_rejects_zero(self):
        s = PriceFeedState()
        s.push(0.0)
        assert s.last_price == 0.0
        assert len(s.ticks) == 0

    def test_rejects_negative(self):
        s = PriceFeedState()
        s.push(-1.0)
        assert s.last_price == 0.0
        assert len(s.ticks) == 0

    def test_accepts_positive(self):
        s = PriceFeedState()
        s.push(82000.0)
        assert s.last_price == 82000.0
        assert len(s.ticks) == 1
