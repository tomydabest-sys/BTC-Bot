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


def parse_bucket_bounds(label: str | None) -> tuple[float, float] | None:
    """Temperature-bucket label -> (lo, hi) in the label's own degree number
    (inclusive-ish). '53°F or below' -> (-inf, 53); '54-55°F' -> (54, 55);
    '84°F or higher' -> (84, inf); '20°C' -> (20, 20). Returns None if
    unparseable. Note the live data only uses 'or below' / 'or higher'; the
    extra synonyms are handled defensively. Bounds are in the label's UNIT
    (°F for US, °C for London/Paris) — callers needing °F must convert."""
    if not label:
        return None
    import re
    s = label.replace("°F", "").replace("°", "").strip().lower()
    if any(w in s for w in ("below", "lower", "under")):
        m = re.search(r"-?\d+", s)
        return (float("-inf"), float(m.group())) if m else None
    if any(w in s for w in ("above", "higher", "over")):
        m = re.search(r"-?\d+", s)
        return (float(m.group()), float("inf")) if m else None
    # range "A-B" — explicit separator so the middle hyphen isn't read as a sign
    m = re.search(r"(-?\d+)\s*-\s*(-?\d+)", s)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r"-?\d+", s)
    return (float(m.group()), float(m.group())) if m else None


def detect_unit(label: str | None) -> str:
    """Return the temperature unit a bucket label is expressed in.

    London & Paris daily-temperature markets use single-degree **°C** buckets
    (e.g. '20°C'); US cities (NYC/Chicago/Miami) use 2°F ranges (e.g. '54-55°F').
    Defaults to 'F' (the original assumption) when no °C marker is present."""
    return "C" if (label and "°c" in label.lower()) else "F"


def _to_unit(temp_f: float, unit: str) -> float:
    """Convert a Fahrenheit reading into the bucket label's own unit."""
    return (temp_f - 32.0) * 5.0 / 9.0 if unit == "C" else float(temp_f)


def _round_half_up(x: float) -> int:
    """Round to the nearest whole degree, halves up — matches how an integer
    daily-high is reported, and avoids Python's banker's-rounding surprises in
    tests. (In practice obs are already quantised to whole °F / whole °C, so this
    only matters at exact .5 boundaries.)"""
    import math
    return math.floor(x + 0.5)


def temp_to_bucket(temp_f: float, buckets: list) -> str | None:
    """Map a Fahrenheit temperature to the matching bucket LABEL, unit-aware.

    `buckets` is a list of ``(label, bounds)`` where ``bounds`` is the output of
    :func:`parse_bucket_bounds` for that label — i.e. the raw numbers from the
    label, in the LABEL's own unit (°F for US, °C for London/Paris). We convert
    ``temp_f`` into each label's unit, round to a whole degree (the resolver
    reports integer-degree highs), and match:

      * range  'A-B'        -> lo <= r <= hi
      * 'or below'          -> r <= hi   (lo = -inf)
      * 'or above'/'higher' -> r >= lo   (hi = +inf)
      * single °C value     -> r == lo (== hi)

    A clean neg-risk partition has exactly one match; if an open-ended tail and a
    finite bucket both match (overlapping inputs), the finite one is preferred.
    Returns the matched label, or None if nothing matches."""
    finite_match = None
    open_match = None
    for label, bounds in buckets:
        if not bounds:
            continue
        lo, hi = bounds
        r = _round_half_up(_to_unit(temp_f, detect_unit(label)))
        if lo <= r <= hi:
            if lo == float("-inf") or hi == float("inf"):
                open_match = open_match or label
            else:
                finite_match = finite_match or label
    return finite_match or open_match


def trade_uid(row: dict) -> str:
    """Deterministic id for dedupe/idempotency. The Data API gives one row per
    fill with no log index, so we hash the identifying fields."""
    key = "|".join(str(row.get(k, "")) for k in (
        "transactionHash", "asset", "proxyWallet", "side", "size", "price", "timestamp",
    ))
    return hashlib.sha256(key.encode()).hexdigest()[:32]
