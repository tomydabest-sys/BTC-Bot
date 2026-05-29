"""Pure parsing/normalisation helpers — unit-tested in tests/test_normalize.py.

These are the functions where silent bugs hide (stringified arrays, decimals,
dedupe keys), so they are kept pure and side-effect free.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any


def parse_json_array(value: Any) -> list:
    """Gamma returns clobTokenIds/outcomes/outcomePrices as STRINGIFIED JSON
    arrays. Accept either a real list or a JSON string; return a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return []
    return [value]


def station_from_resolution(url: str | None) -> str | None:
    """Resolution source is a Wunderground URL ending in a station code, e.g.
    .../new-york-city/KLGA -> 'KLGA'. Returns None if not parseable."""
    if not url or not isinstance(url, str):
        return None
    tail = url.rstrip("/").split("/")[-1].strip()
    # station codes are short alphanumerics (e.g. KLGA, EGLL); guard against
    # picking up a slug word.
    if tail and tail.isalnum() and tail.upper() == tail and 3 <= len(tail) <= 5:
        return tail
    return None


def build_event_slug(template: str, city_slug: str, d: date) -> str:
    """Daily-temperature slug, e.g. highest-temperature-in-nyc-on-may-20-2026.
    Month is full lowercase name, day has no leading zero (verified live)."""
    return template.format(
        city=city_slug,
        month=d.strftime("%B").lower(),
        day=str(d.day),
        year=d.year,
    )


def usdc_notional(size: float, price: float) -> float:
    """USDC notional of a Data-API fill. Data-API decimals are human-readable,
    so this is simply size * price (no 1e6 scaling)."""
    return round(float(size) * float(price), 6)


def iso_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def trade_uid(row: dict) -> str:
    """Deterministic id for dedupe/idempotency. The Data API gives one row per
    fill with no log index, so we hash the identifying fields."""
    key = "|".join(str(row.get(k, "")) for k in (
        "transactionHash", "asset", "proxyWallet", "side", "size", "price", "timestamp",
    ))
    return hashlib.sha256(key.encode()).hexdigest()[:32]
