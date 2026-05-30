"""Unit tests for parsing/normalisation math (offline)."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.common import normalize as N  # noqa: E402


def test_parse_json_array_handles_stringified_and_real():
    assert N.parse_json_array('["a","b"]') == ["a", "b"]
    assert N.parse_json_array(["a", "b"]) == ["a", "b"]
    assert N.parse_json_array(None) == []
    assert N.parse_json_array("not json") == []


def test_station_from_resolution():
    url = "https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA"
    assert N.station_from_resolution(url) == "KLGA"
    assert N.station_from_resolution(url + "/") == "KLGA"
    assert N.station_from_resolution("https://example.com/new-york-city") is None
    assert N.station_from_resolution(None) is None


def test_build_event_slug_matches_live_format():
    s = N.build_event_slug(
        "highest-temperature-in-{city}-on-{month}-{day}-{year}", "nyc", date(2026, 5, 20))
    assert s == "highest-temperature-in-nyc-on-may-20-2026"
    # no leading zero on day; full lowercase month
    s2 = N.build_event_slug(
        "highest-temperature-in-{city}-on-{month}-{day}-{year}", "nyc", date(2026, 4, 5))
    assert s2 == "highest-temperature-in-nyc-on-april-5-2026"


def test_usdc_notional_no_1e6_scaling():
    # size in shares, price in [0,1] -> plain product (Data API decimals)
    assert N.usdc_notional(7.04, 0.999) == round(7.04 * 0.999, 6)
    assert N.usdc_notional(100, 0.5) == 50.0


def test_trade_uid_stable_and_sensitive():
    row = {"transactionHash": "0xabc", "asset": "123", "proxyWallet": "0xw",
           "side": "BUY", "size": 5, "price": 0.1, "timestamp": 1776319882}
    a = N.trade_uid(row)
    b = N.trade_uid(dict(row))
    assert a == b and len(a) == 32
    row2 = dict(row); row2["price"] = 0.2
    assert N.trade_uid(row2) != a


def test_iso_from_unix():
    assert N.iso_from_unix(1776319882).startswith("2026-")


def test_detect_unit():
    assert N.detect_unit("20°C") == "C"
    assert N.detect_unit("20°C or below") == "C"
    assert N.detect_unit("54-55°F") == "F"
    assert N.detect_unit("84°F or above") == "F"
    assert N.detect_unit(None) == "F"          # default to the original assumption


def _buckets(labels):
    return [(lbl, N.parse_bucket_bounds(lbl)) for lbl in labels]


def test_temp_to_bucket_fahrenheit_ranges():
    # US partition: tail + 2°F ranges + tail
    us = _buckets(["53°F or below", "54-55°F", "56-57°F", "82-83°F", "84°F or above"])
    assert N.temp_to_bucket(84.2, us) == "84°F or above"   # round 84 -> top tail (no 84-85 here)
    assert N.temp_to_bucket(75.0, _buckets(["74-75°F", "76-77°F"])) == "74-75°F"
    assert N.temp_to_bucket(54.4, us) == "54-55°F"          # round 54
    assert N.temp_to_bucket(50.0, us) == "53°F or below"    # below the lowest range
    assert N.temp_to_bucket(95.0, us) == "84°F or above"


def test_temp_to_bucket_celsius_single_degree():
    # London/Paris partition: single-degree °C buckets + open tails
    eu = _buckets(["16°C or below", "17°C", "18°C", "19°C", "20°C", "21°C", "22°C or higher"])
    assert N.temp_to_bucket(68.0, eu) == "20°C"   # 68F == 20C exactly
    assert N.temp_to_bucket(69.8, eu) == "21°C"   # 69.8F == 21C exactly
    assert N.temp_to_bucket(66.2, eu) == "19°C"   # 66.2F == 19C
    assert N.temp_to_bucket(60.0, eu) == "16°C or below"   # 15.55C -> 16 -> bottom tail
    assert N.temp_to_bucket(75.2, eu) == "22°C or higher"  # 24C -> top tail


def test_temp_to_bucket_prefers_finite_over_open_tail():
    # overlapping input (not a clean partition): the finite bucket wins
    mixed = _buckets(["20°C", "20°C or below"])
    assert N.temp_to_bucket(68.0, mixed) == "20°C"


def test_temp_to_bucket_no_match_returns_none():
    assert N.temp_to_bucket(40.0, _buckets(["54-55°F", "56-57°F"])) is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} normalize tests passed")
