"""Phase 8 driver — forward live-book capture (standalone long-running collector).

    python scripts/run_phase8.py                      # run until Ctrl-C (cadence from config)
    python scripts/run_phase8.py --duration 3600       # run for 1 hour
    python scripts/run_phase8.py --max-cycles 2 --cadence 3   # bounded smoke test

Backfills the same processed/ SQLite (book_snapshots + book_levels) so Phases
3-6 can later run over real quoting data. Safe to stop/restart at any time.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ingest.clob_live_capture import run_collector  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-cycles", type=int, default=None)
    ap.add_argument("--duration", type=int, default=None, help="seconds")
    ap.add_argument("--cadence", type=int, default=None, help="seconds between snapshots")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")
    try:
        res = run_collector(max_cycles=args.max_cycles, duration_s=args.duration,
                            cadence_s=args.cadence)
    except KeyboardInterrupt:
        print("\nstopped by user")
        return 0
    print(f"done: {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
