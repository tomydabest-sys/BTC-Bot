"""Live entry point.

Refuses to start unless BOT_MODE=live AND --confirm-live is passed AND the
validation gate is green. Override only via --force-live with an explicit
small --bankroll-cap-usdc.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

import structlog
import yaml

from polybot.polyweather.data.stations.station_resolver import StationResolver
from polybot.polyweather.orchestrator.engine import (
    EngineConfig,
    PolyWeatherEngine,
    build_validation_gate,
)
from polybot.polyweather.persistence.store import PolyWeatherStore
from polybot.polyweather.risk.weather_risk import WeatherRiskConfig

logger = structlog.get_logger()

REPO_ROOT = Path(__file__).resolve().parents[2]
RISK_PATH = REPO_ROOT / "config" / "polyweather" / "risk.yaml"
MARKETS_PATH = REPO_ROOT / "config" / "polyweather" / "markets.yaml"
WEIGHTS_PATH = REPO_ROOT / "config" / "polyweather" / "strategy_weights.yaml"
LIVE_DB = REPO_ROOT / "data" / "runtime" / "polyweather" / "live.sqlite"
PAPER_DB = REPO_ROOT / "data" / "runtime" / "polyweather" / "paper.sqlite"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--confirm-live", action="store_true", required=False)
    p.add_argument("--force-live", action="store_true")
    p.add_argument("--bankroll-cap-usdc", type=Decimal, default=None)
    p.add_argument("--cycle-seconds", type=float, default=5.0)
    p.add_argument("--duration", type=float, default=None)
    return p.parse_args(argv)


def gate_blocks_live(paper_db: Path) -> tuple[bool, dict]:
    if not paper_db.exists():
        return True, {"pass": False, "criteria": {}, "reason": "paper db missing"}
    store = PolyWeatherStore(paper_db)
    resolver = StationResolver()
    gate = build_validation_gate(store, resolver)
    return not gate["pass"], gate


async def run(args: argparse.Namespace) -> int:
    if os.environ.get("BOT_MODE", "").lower() != "live":
        print("ERROR: BOT_MODE must be 'live' to use live_run.py", file=sys.stderr)
        return 2
    if not args.confirm_live:
        print("ERROR: --confirm-live flag is required", file=sys.stderr)
        return 2

    blocked, gate = gate_blocks_live(PAPER_DB)
    if blocked and not args.force_live:
        print("VALIDATION GATE BLOCKS LIVE TRADING", file=sys.stderr)
        for name, c in gate.get("criteria", {}).items():
            mark = "PASS" if c.get("pass") else "FAIL"
            print(f"  [{mark}] {name}: {c.get('reason')}", file=sys.stderr)
        return 2

    if args.force_live and args.bankroll_cap_usdc is None:
        print("ERROR: --force-live requires --bankroll-cap-usdc", file=sys.stderr)
        return 2

    risk = WeatherRiskConfig.from_yaml(yaml.safe_load(RISK_PATH.read_text()))
    if args.bankroll_cap_usdc is not None:
        risk.bankroll_usdc = args.bankroll_cap_usdc

    engine_cfg = EngineConfig.from_files(
        risk_yaml=RISK_PATH,
        markets_yaml=MARKETS_PATH,
        weights_yaml=WEIGHTS_PATH,
        mode="live",
        use_mock=False,
        cycle_seconds=args.cycle_seconds,
        duration_seconds=args.duration,
    )
    engine_cfg.risk = risk
    engine_cfg.bankroll_cap_usdc = args.bankroll_cap_usdc

    store = PolyWeatherStore(LIVE_DB)
    resolver = StationResolver()
    engine = PolyWeatherEngine(engine_cfg, store=store, station_resolver=resolver)
    engine.risk.begin_live_session()

    print("LIVE engine starting — hard $5 first-24h position cap is in effect.")
    try:
        await engine.start()
    finally:
        await engine.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
