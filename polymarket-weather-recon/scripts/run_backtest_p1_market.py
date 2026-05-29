"""Market-price-triggered P1 backtest driver.

    python scripts/run_backtest_p1_market.py
Requires Phases 1-2 (trades + resolved markets). No weather reference needed.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analyze.backtest_p1_market import run_market_backtest  # noqa: E402


def main() -> int:
    logging.basicConfig(level="WARNING")
    res = run_market_backtest()
    print(json.dumps(res, indent=2))
    print("\nwrote reports/backtest_p1_market.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
