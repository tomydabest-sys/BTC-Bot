"""Phase 2 driver — ingest trades for discovered markets + data-quality report.

    python scripts/run_phase2.py             # ingest, skipping markets already done
    python scripts/run_phase2.py --cache-bust
Run scripts/run_phase1.py first to populate the markets table.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ingest.data_api_trades import ingest_discovered_markets  # noqa: E402
from src.report.data_quality import compute, write_markdown  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-bust", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    totals = ingest_discovered_markets(cache_bust=args.cache_bust)
    print("ingestion totals:", totals)

    stats = compute()
    path = write_markdown(stats)
    print("\n=== data-quality summary ===")
    print(json.dumps({k: v for k, v in stats.items()
                      if k not in ("top_markets", "possible_truncation_markets")}, indent=2))
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
