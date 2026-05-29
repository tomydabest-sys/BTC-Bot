"""Phase 3 (+3.5) driver — reference signal + per-wallet feature engineering.

    python scripts/run_phase3.py
    python scripts/run_phase3.py --min-trades 5
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ingest.reference_feed import build_reference_for_window  # noqa: E402
from src.features.wallet_features import build_wallet_features  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-trades", type=int, default=1)
    ap.add_argument("--cache-bust", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s %(message)s")

    ref = build_reference_for_window(cache_bust=args.cache_bust)
    print("reference points by station:", ref)

    feats = build_wallet_features(min_trades=args.min_trades)
    print(f"\nwallet feature rows: {len(feats)}  (cols: {len(feats.columns)})")
    print("columns:", list(feats.columns))
    # quick distribution sanity on key automation features
    for col in ("n_trades", "cv_gap", "hour_entropy", "active_hours",
                "reaction_alignment", "realized_pnl_usdc", "breadth_markets"):
        if col in feats:
            s = feats[col].describe()
            print(f"  {col:24s} median={s.get('50%', float('nan')):.3f} "
                  f"max={s.get('max', float('nan')):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
