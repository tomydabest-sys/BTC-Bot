"""Phase 1 driver — discover weather markets for the configured window.

    python scripts/run_phase1.py            # bounded_window from config
    python scripts/run_phase1.py --window scale_up_window
    python scripts/run_phase1.py --cache-bust
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ingest.gamma_markets import discover_weather_markets  # noqa: E402
from src.common.config import project_root  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", default="bounded_window")
    ap.add_argument("--cache-bust", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    rows = discover_weather_markets(cache_bust=args.cache_bust, window=args.window)
    events = sorted({r["event_slug"] for r in rows})
    review = [r for r in rows if r.get("needs_review")]
    print(f"discovered {len(rows)} bucket-markets across {len(events)} events")
    print(f"  -> reports/weather_markets.csv")
    print(f"  -> {len(review)} flagged needs_review (missing station/condition id)")
    stations = sorted({r["station"] for r in rows if r["station"]})
    print(f"  stations: {stations}")
    print(f"  sample events: {events[:5]}")
    assert (project_root() / "reports" / "weather_markets.csv").exists()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
