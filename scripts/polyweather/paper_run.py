"""Paper-trading entry point.

Usage:
  python scripts/polyweather/paper_run.py                   # live APIs, paper fills
  python scripts/polyweather/paper_run.py --mock            # fixtures, paper fills
  python scripts/polyweather/paper_run.py --duration 60     # run for fixed wall-time (seconds)
  python scripts/polyweather/paper_run.py --reset           # wipe paper SQLite first
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

import structlog
import uvicorn

from polybot.polyweather.dashboard.routes import create_app
from polybot.polyweather.data.stations.station_resolver import StationResolver
from polybot.polyweather.orchestrator.engine import EngineConfig, PolyWeatherEngine
from polybot.polyweather.persistence.store import PolyWeatherStore

logger = structlog.get_logger()

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RISK = REPO_ROOT / "config" / "polyweather" / "risk.yaml"
DEFAULT_MARKETS = REPO_ROOT / "config" / "polyweather" / "markets.yaml"
DEFAULT_WEIGHTS = REPO_ROOT / "config" / "polyweather" / "strategy_weights.yaml"
DEFAULT_DB = REPO_ROOT / "data" / "runtime" / "polyweather" / "paper.sqlite"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--mock", action="store_true", help="Use fixtures instead of live APIs")
    p.add_argument("--duration", type=float, default=None, help="Wall-time seconds to run")
    p.add_argument("--cycle-seconds", type=float, default=5.0, help="Seconds between cycles")
    p.add_argument("--reset", action="store_true", help="Wipe paper SQLite first")
    p.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8080")))
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    return p.parse_args(argv)


def _reset_db(path: Path) -> None:
    if path.exists():
        path.unlink()
        logger.info("paper_db_reset", path=str(path))


async def run(args: argparse.Namespace) -> int:  # noqa: C901
    use_mock = args.mock or os.environ.get("BOT_MOCK_DATA", "").lower() == "true"

    if args.reset:
        _reset_db(args.db)

    engine_cfg = EngineConfig.from_files(
        risk_yaml=DEFAULT_RISK,
        markets_yaml=DEFAULT_MARKETS,
        weights_yaml=DEFAULT_WEIGHTS,
        mode="paper",
        use_mock=use_mock,
        cycle_seconds=args.cycle_seconds,
        duration_seconds=args.duration,
    )

    store = PolyWeatherStore(args.db)
    resolver = StationResolver()
    engine = PolyWeatherEngine(engine_cfg, store=store, station_resolver=resolver)

    app = create_app(
        engine=engine, store=store, station_resolver=resolver,
        mode="paper", mock=use_mock,
    )

    dashboard_task: asyncio.Task | None = None
    if not args.no_dashboard:
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        dashboard_task = asyncio.create_task(server.serve(), name="polyweather_dashboard")

    stop_event = asyncio.Event()

    def _request_stop(*_: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig_name), _request_stop)
        except (NotImplementedError, RuntimeError):
            pass

    engine_task = asyncio.create_task(engine.start(), name="polyweather_engine")

    try:
        if args.duration is not None:
            await asyncio.wait_for(asyncio.shield(engine_task), timeout=args.duration + 5)
        else:
            done, _ = await asyncio.wait(
                [engine_task, asyncio.create_task(stop_event.wait())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for d in done:
                exc = d.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    raise exc
    finally:
        await engine.shutdown()
        if not engine_task.done():
            engine_task.cancel()
            try:
                await engine_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if dashboard_task is not None:
            dashboard_task.cancel()
            try:
                await dashboard_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
