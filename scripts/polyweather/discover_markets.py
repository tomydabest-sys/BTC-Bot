"""One-shot Gamma scan + station mapping audit.

  python scripts/polyweather/discover_markets.py [--mock]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from polybot.polyweather.data.stations.station_resolver import StationResolver
from polybot.polyweather.exchanges.gamma_client import GammaClient, MockGammaClient


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--mock", action="store_true")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    use_mock = args.mock or os.environ.get("BOT_MOCK_DATA", "").lower() == "true"
    client = MockGammaClient() if use_mock else GammaClient()
    resolver = StationResolver()
    events = await client.list_active_weather_markets()
    print(f"{len(events)} active weather events")
    print(f"{'event_id':40}  {'station':6}  {'verified':9}  title")
    for ev in events:
        st = resolver.resolve(ev.id, ev.rules)
        print(
            f"{ev.id[:40]:40}  {(st.icao if st else '—'):6}  "
            f"{'pending':9}  {ev.title[:60]}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
