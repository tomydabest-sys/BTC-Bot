"""Phase 0 smoke tests — config loads, conventions are sane, http cache key is
stable. Network is NOT touched here (offline-safe). Run: python -m pytest -q
(or: python tests/test_smoke.py for a no-pytest fallback).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common import config, http  # noqa: E402


def test_config_loads_and_has_core_sections():
    cfg = config.load_config()
    for section in ("sources", "weather_reference", "weather_discovery",
                    "conventions", "ingestion", "storage", "thresholds"):
        assert section in cfg, f"missing config section: {section}"


def test_reachability_flags_match_phase0_findings():
    r = config.reachable_sources()
    # Verified live during Phase 0:
    assert r["gamma"] is True
    assert r["data_api"] is True
    assert r["clob"] is True
    assert r["subgraph_goldsky"] is False     # blocked by allowlist
    assert r["onchain_polygon"] is False       # blocked by allowlist
    assert r["weather:open_meteo"] is True
    assert r["weather:nws"] is True
    assert r["weather:iem"] is True            # unblocked 2026-05-30 (allowlist add)


def test_price_convention():
    conv = config.load_config()["conventions"]
    assert conv["price_range"] == [0.0, 1.0]
    assert conv["min_tick"] == 0.001
    assert "shares" in conv["size_unit"]


def test_cache_key_is_deterministic_and_param_sensitive():
    k1 = http._cache_key("GET", "https://x/y", {"a": 1, "b": 2}, None)
    k2 = http._cache_key("GET", "https://x/y", {"b": 2, "a": 1}, None)  # order-insensitive
    k3 = http._cache_key("GET", "https://x/y", {"a": 1, "b": 3}, None)  # different params
    assert k1 == k2
    assert k1 != k3


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} smoke tests passed")
