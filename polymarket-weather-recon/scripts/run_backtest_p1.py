"""P1 resolution-drift backtest driver.

    python scripts/run_backtest_p1.py
Requires Phases 1-3 outputs (trades, resolved markets, reference_temp).
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analyze.backtest_p1 import run_backtest  # noqa: E402


def main() -> int:
    logging.basicConfig(level="WARNING")
    res = run_backtest()
    print(json.dumps(res, indent=2))
    print("\nwrote reports/backtest_p1.md (+ backtest_p1_entries.csv)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
