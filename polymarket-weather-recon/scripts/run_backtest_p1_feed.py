"""Driver — feed-driven P1 backtest (Option-2).

    python scripts/run_backtest_p1_feed.py --gate-only   # just the 3c accuracy gate
    python scripts/run_backtest_p1_feed.py               # gate, then sim if it passes

Prereqs: run_phase1 (markets), IEM ingest (src.ingest.iem_asos.build_iem_reference_for_window),
and — for the trading sim — run_phase2 (trades).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.analyze.backtest_p1_feed import feed_bucket_accuracy, run  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate-only", action="store_true",
                    help="run only the 3c bucket-accuracy gate")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    if args.gate_only:
        gate = feed_bucket_accuracy()
        if "error" in gate:
            print("ERROR:", gate["error"]); return 1
        summary = {k: v for k, v in gate.items() if k != "records"}
        print(json.dumps(summary, indent=2))
        print(f"\nGATE {'PASSED' if gate['gate_passed'] else 'FAILED'}: overall "
              f"{gate['overall_accuracy']:.1%} vs gate {gate['gate_min_accuracy']:.0%}")
        return 0

    res = run()
    if "error" in res:
        print("ERROR:", res["error"]); return 1
    g = res["gate"]
    print(f"gate: overall {g['overall_accuracy']:.1%} -> "
          f"{'PASS' if g['gate_passed'] else 'FAIL'}")
    if "sim" in res and "error" in res["sim"]:
        print("sim not run:", res["sim"]["error"])
    print("wrote reports/backtest_p1_feed.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
