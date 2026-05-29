"""Phase 6 + 7 driver — exploitable patterns + BTC-Bot proposals.

    python scripts/run_phase67.py
Requires Phases 1-4 outputs in the SQLite db.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analyze.exploits import compute, write_markdown  # noqa: E402
from src.report.build_report import build_recommendations  # noqa: E402


def main() -> int:
    logging.basicConfig(level="WARNING")
    stats = compute()
    ex_path = write_markdown(stats)
    print("=== exploit-supporting stats ===")
    print(json.dumps({k: v for k, v in stats.items() if k != "hourly_volume"}, indent=2))
    print(f"wrote {ex_path}")

    md, js = build_recommendations()
    print(f"\nwrote {md}\nwrote {js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
