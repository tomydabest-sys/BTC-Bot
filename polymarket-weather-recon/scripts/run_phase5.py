"""Phase 5 driver — build top-bot dossiers.

    python scripts/run_phase5.py --top-n 12
Requires Phase 3 + 4 outputs (wallet_features, wallet_classification tables).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analyze.profiler import build_dossiers  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top-n", type=int, default=12)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")
    written = build_dossiers(top_n=args.top_n)
    print(f"wrote {len(written)} dossiers + INDEX.md to reports/dossiers/")
    for w in written:
        print("  -", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
