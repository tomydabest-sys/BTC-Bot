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
import logging
import os
import signal
import sys
import time
from pathlib import Path

import structlog
import uvicorn

from polybot.polyweather.dashboard.routes import create_app
from polybot.polyweather.data.stations.station_resolver import StationResolver
from polybot.polyweather.orchestrator.engine import EngineConfig, PolyWeatherEngine
from polybot.polyweather.persistence.store import PolyWeatherStore

logger = structlog.get_logger()


def _configure_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)-7s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=True),
            structlog.dev.ConsoleRenderer(),
        ],
    )

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RISK = REPO_ROOT / "config" / "polyweather" / "risk.yaml"
DEFAULT_MARKETS = REPO_ROOT / "config" / "polyweather" / "markets.yaml"
DEFAULT_WEIGHTS = REPO_ROOT / "config" / "polyweather" / "strategy_weights.yaml"
DEFAULT_WALLETS = REPO_ROOT / "config" / "polyweather" / "wallets.yaml"
DEFAULT_DB = REPO_ROOT / "data" / "runtime" / "polyweather" / "paper.sqlite"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--mock", action="store_true", help="Use fixtures instead of live APIs")
    p.add_argument(
        "--live-data",
        action="store_true",
        help=(
            "Use REAL Polymarket Gamma + real forecast APIs but PAPER-FILL "
            "trades (no real money). Required for the 14-day validation gate."
        ),
    )
    p.add_argument("--duration", type=float, default=None, help="Wall-time seconds to run")
    p.add_argument("--cycle-seconds", type=float, default=5.0, help="Seconds between cycles")
    p.add_argument("--reset", action="store_true", help="Wipe paper SQLite first")
    p.add_argument(
        "--keep-state",
        action="store_true",
        help="Do NOT auto-wipe in --mock mode (default is fresh state per run)",
    )
    p.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8080")))
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument(
        "--log-level",
        default=os.environ.get("BOT_LOG_LEVEL", "INFO"),
        help="DEBUG / INFO / WARNING / ERROR",
    )
    p.add_argument(
        "--status-every",
        type=float,
        default=10.0,
        help="Seconds between console status lines (0 to disable)",
    )
    p.add_argument(
        "--bucket-cooldown",
        type=float,
        default=None,
        help="Seconds before the same bucket can trade again (default 300 live, 30 mock)",
    )
    p.add_argument(
        "--position-horizon",
        type=float,
        default=None,
        help="Seconds a paper position is held open before settlement (default 60 mock)",
    )
    return p.parse_args(argv)


def _reset_db(path: Path) -> None:
    """Wipe the paper SQLite and the station audit log together.

    Both files persist across sessions; wiping only one leads to stale
    audit entries (the user saw 1326 'verified' rows accumulate across
    several mock runs).
    """
    for f in (path, path.with_suffix(".sqlite-wal"), path.with_suffix(".sqlite-shm")):
        if f.exists():
            f.unlink()
            logger.info("paper_db_reset", path=str(f))
    audit_path = path.parent / "station_audit.jsonl"
    if audit_path.exists():
        audit_path.unlink()
        logger.info("station_audit_reset", path=str(audit_path))


async def _status_loop(engine, store, interval_s: float, stop_event: asyncio.Event) -> None:
    """Print a one-line console summary every ``interval_s`` seconds."""
    started = time.time()
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            return
        except TimeoutError:
            pass
        m = engine.metrics
        risk = engine.risk
        halted, halt_reason = risk.check_kill_switch()
        line = (
            f"[{time.strftime('%H:%M:%S')}] "
            f"cycles={m.cycles} signals={m.signals_total} fills={m.fills_total} "
            f"bankroll=${risk.state.current_bankroll:.2f} "
            f"daily_pnl=${risk.state.daily_pnl:.2f} "
            f"halted={'YES('+halt_reason+')' if halted else 'no'} "
            f"trades_in_db={store.trade_count()}"
        )
        print(line, flush=True)
        if time.time() - started > 1.5 * interval_s and m.cycles == 0:
            print(
                "  ⚠ no cycles run yet — check that fixtures exist under "
                "tests/fixtures/polyweather/",
                flush=True,
            )


async def run(args: argparse.Namespace) -> int:  # noqa: C901
    _configure_logging(args.log_level)
    env_mock = os.environ.get("BOT_MOCK_DATA", "").lower() == "true"
    use_mock = (args.mock or env_mock) and not args.live_data
    live_data = bool(args.live_data)

    # Auto-reset for --mock runs so every demo starts at the configured
    # bankroll. Override with --keep-state if the operator wants to
    # continue a previous mock session. --live-data NEVER auto-resets
    # because the 14-day validation gate needs continuous history.
    should_reset = args.reset or (use_mock and not args.keep_state)
    if should_reset:
        _reset_db(args.db)

    data_label = "MOCK" if use_mock else ("LIVE-DATA" if live_data else "LIVE")
    print(
        f"polyweather: starting paper bot — data={data_label} duration={args.duration} "
        f"cycle={args.cycle_seconds}s db={args.db}",
        flush=True,
    )
    if live_data:
        print(
            "polyweather: ⚠ LIVE-DATA mode: reads REAL Polymarket + forecast APIs,",
            flush=True,
        )
        print(
            "             paper-fills only — no real orders are placed.",
            flush=True,
        )

    engine_cfg = EngineConfig.from_files(
        risk_yaml=DEFAULT_RISK,
        markets_yaml=DEFAULT_MARKETS,
        weights_yaml=DEFAULT_WEIGHTS,
        wallets_yaml=DEFAULT_WALLETS,
        mode="paper",
        use_mock=use_mock,
        live_data=live_data,
        cycle_seconds=args.cycle_seconds,
        duration_seconds=args.duration,
    )
    if args.bucket_cooldown is not None:
        engine_cfg.bucket_cooldown_seconds = args.bucket_cooldown
    elif use_mock:
        engine_cfg.bucket_cooldown_seconds = 30.0
    if args.position_horizon is not None:
        engine_cfg.position_horizon_seconds = args.position_horizon

    # In mock mode the spec 30-min consecutive-loss pause is longer than any
    # reasonable demo run. Scale it down so a temporary halt actually clears
    # during the session and the dashboard doesn't sit on a red banner.
    if use_mock:
        engine_cfg.risk.consecutive_loss_pause_seconds = 30.0
        engine_cfg.risk.daily_loss_cooldown_seconds = 60.0

    store = PolyWeatherStore(args.db)
    resolver = StationResolver(audit_enabled=not use_mock)
    engine = PolyWeatherEngine(engine_cfg, store=store, station_resolver=resolver)

    app = create_app(
        engine=engine, store=store, station_resolver=resolver,
        mode="paper", mock=use_mock,
    )

    dashboard_task: asyncio.Task | None = None
    server: uvicorn.Server | None = None
    if not args.no_dashboard:
        # ``lifespan="off"`` skips the starlette lifespan task whose forced
        # cancellation produced the misleading "CancelledError" traceback on
        # graceful shutdown.
        config = uvicorn.Config(
            app, host=args.host, port=args.port,
            log_level="warning", access_log=False, lifespan="off",
        )
        server = uvicorn.Server(config)
        dashboard_task = asyncio.create_task(server.serve(), name="polyweather_dashboard")
        print(
            f"polyweather: dashboard → http://{args.host}:{args.port}",
            flush=True,
        )
    else:
        print("polyweather: --no-dashboard set, skipping HTTP server", flush=True)

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
    stop_task = asyncio.create_task(stop_event.wait(), name="polyweather_stop")
    status_task: asyncio.Task | None = None
    if args.status_every > 0:
        status_task = asyncio.create_task(
            _status_loop(engine, store, args.status_every, stop_event),
            name="polyweather_status",
        )

    try:
        # Always race the engine task against an explicit stop event. The
        # engine respects --duration internally so we don't need a separate
        # timeout; Ctrl-C on Windows raises KeyboardInterrupt and is caught
        # in main() for a clean exit.
        done, _ = await asyncio.wait(
            [engine_task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for d in done:
            if d is stop_task:
                continue
            exc = d.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                raise exc
    finally:
        stop_task.cancel()
        if status_task is not None:
            status_task.cancel()
        await engine.shutdown()
        if not engine_task.done():
            engine_task.cancel()
            try:
                await engine_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if dashboard_task is not None:
            # Ask uvicorn to shut down gracefully; only force-cancel if it
            # doesn't oblige within 3s. This avoids the CancelledError noise
            # users were seeing during normal exit.
            if server is not None:
                server.should_exit = True
            try:
                await asyncio.wait_for(dashboard_task, timeout=3.0)
            except (TimeoutError, asyncio.CancelledError):
                dashboard_task.cancel()
                try:
                    await dashboard_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass

    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\npolyweather: stopped by user (Ctrl-C).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
