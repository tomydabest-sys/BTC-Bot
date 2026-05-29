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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} normalize tests passed")
