"""Report paper-trade win-rate and P&L by entry-price band.

The weather-wallet brief's go/no-go before risking real money: confirm the bot
makes money in the uncertain middle (~15-85c) and isn't bleeding in the cheap
long-shot tail. Run this against the paper SQLite after (or during) a paper
session.

Usage:
  python scripts/polyweather/analyze_bands.py
  python scripts/polyweather/analyze_bands.py --db path/to/paper.sqlite --limit 100000
  python scripts/polyweather/analyze_bands.py --strategy weather_ensemble
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from polybot.polyweather.analysis.band_report import band_report, format_report  # noqa: E402
from polybot.polyweather.persistence.store import PolyWeatherStore  # noqa: E402

DEFAULT_DB = REPO_ROOT / "data" / "runtime" / "polyweather" / "paper.sqlite"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--limit", type=int, default=100_000)
    p.add_argument("--strategy", default=None, help="Filter to one strategy")
    p.add_argument("--city", default=None, help="Filter to one city")
    args = p.parse_args()

    if not args.db.exists():
        print(f"no paper db at {args.db} — run the bot first.")
        return 1

    store = PolyWeatherStore(args.db)
    trades = store.trades(limit=args.limit, strategy=args.strategy, city=args.city)
    rep = band_report(trades)
    print(format_report(rep))
    if rep.by_strategy:
        print("\nP&L by strategy:")
        for s, v in sorted(rep.by_strategy.items(), key=lambda kv: kv[1], reverse=True):
            print(f"  {s:<22} ${float(v):>10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
