"""Dashboard + bot launcher.

Runs the FastAPI dashboard alongside the trading bot in a single process.
Both processes share the same storage backend so the dashboard sees live data.

Adds --mock-btc-feed flag for offline/testing runs (sets BTC_BOT_USE_MOCK_FEED).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import structlog
import uvicorn

from polybot.config import load_config
from polybot.main import Bot


def _configure_logging(level: str) -> None:
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


class DualOutputLogger:
    """Tee stdout/stderr to a log file while preserving console output."""

    def __init__(self, original_stream, log_path: Path):
        self._stream = original_stream
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(log_path, "a", encoding="utf-8", buffering=1)

    def write(self, msg: str) -> int:
        try:
            self._stream.write(msg)
        except Exception:
            pass
        try:
            self._fh.write(msg)
        except Exception:
            pass
        return len(msg)

    def flush(self) -> None:
        try:
            self._stream.flush()
        except Exception:
            pass
        try:
            self._fh.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        try:
            return self._stream.isatty()
        except Exception:
            return False

    def fileno(self) -> int:
        return self._stream.fileno()

    def __getattr__(self, name):
        # Delegate any other stream attributes (encoding, closed, etc.) to the
        # wrapped stream so libraries inspecting sys.stdout don't crash.
        return getattr(self._stream, name)


def validate_system(config) -> bool:
    """Pre-flight checks. Returns True if OK to start."""
    log = structlog.get_logger()
    issues: list[str] = []

    # Check data dir exists / is writeable
    data_dir = Path(config.bot.data_dir)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / ".write_test").write_text("ok")
        (data_dir / ".write_test").unlink()
    except Exception as e:
        issues.append(f"data_dir not writeable: {data_dir}: {e}")

    # Check API key envs
    api_key = os.environ.get(config.wallet.api_key_env, "")
    if not api_key:
        log.warning(
            "no_api_key",
            env_var=config.wallet.api_key_env,
            note="Polymarket Gamma API still works without one but rate limits are tighter",
        )

    # Live mode hard guard
    if config.bot.mode == "live" and not config.bot.allow_live:
        issues.append(
            "config.bot.mode='live' but config.bot.allow_live=false. "
            "Live mode is hard-guarded; you must opt in explicitly."
        )

    if issues:
        for issue in issues:
            log.error("preflight_failed", issue=issue)
        return False
    return True


async def _run_with_dashboard(args, config) -> None:
    log = structlog.get_logger()

    bot = Bot(config)
    stop_event = asyncio.Event()

    def _on_signal(*_):
        log.info("signal_received_stopping")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stop_event.set())

    bot_task: asyncio.Task | None = None
    server_task: asyncio.Task | None = None

    try:
        if not args.no_bot:
            await bot.start()
            bot_task = asyncio.create_task(stop_event.wait())

        if not args.no_dashboard:
            from polybot.dashboard.app import create_app
            app = create_app(bot=bot if not args.no_bot else None, config=config)
            ucfg = uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                log_level=args.log_level.lower(),
                access_log=False,
            )
            server = uvicorn.Server(ucfg)
            server_task = asyncio.create_task(server.serve())
            log.info("dashboard_started", url=f"http://{args.host}:{args.port}")

        # Wait for stop
        await stop_event.wait()
    finally:
        if not args.no_bot:
            try:
                await bot.stop()
            except Exception as e:
                log.warning("bot_stop_err", error=str(e))
        if server_task is not None:
            server_task.cancel()
            try:
                await server_task
            except (asyncio.CancelledError, Exception):
                pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="BTC-Bot launcher — dashboard + bot in one process",
    )
    parser.add_argument("--config", default="config.yaml", help="Config YAML path")
    parser.add_argument("--mode", choices=["paper", "live"], default=None,
                        help="Override config.bot.mode")
    parser.add_argument("--host", default="0.0.0.0", help="Dashboard bind host")
    parser.add_argument("--port", type=int, default=8080, help="Dashboard bind port")
    parser.add_argument("--log-level", default="INFO",
                        help="Log level (DEBUG, INFO, WARNING, ERROR)")
    parser.add_argument("--log-file", default="logs/bot.log",
                        help="Tee stdout/stderr into this file")
    parser.add_argument("--no-bot", action="store_true",
                        help="Run dashboard only, no trading")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Run bot only, no dashboard")
    parser.add_argument("--mock-btc-feed", action="store_true",
                        help="Use deterministic synthetic BTC feed (no Binance)")
    parser.add_argument("--no-validate", action="store_true",
                        help="Skip pre-flight validation")
    args = parser.parse_args()

    # Tee output
    log_file = Path(args.log_file)
    sys.stdout = DualOutputLogger(sys.stdout, log_file)
    sys.stderr = DualOutputLogger(sys.stderr, log_file)

    # Wire mock feed BEFORE constructing the bot
    if args.mock_btc_feed:
        os.environ["BTC_BOT_USE_MOCK_FEED"] = "1"

    _configure_logging(args.log_level)
    log = structlog.get_logger()
    log.info("launcher_starting", config=args.config)

    config = load_config(args.config)

    if args.mode is not None:
        config.bot.mode = args.mode

    # Always require explicit --mode=live and matching config.allow_live
    if not args.no_validate and not validate_system(config):
        log.error("preflight_failed_aborting")
        sys.exit(1)

    try:
        asyncio.run(_run_with_dashboard(args, config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
