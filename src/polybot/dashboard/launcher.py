"""Launcher that runs the bot and dashboard together.

Hardened:
- DualOutputLogger now accepts *args/**kwargs from structlog's factory protocol
  (prevents AttributeError if any future code calls structlog.get_logger("name")).
- Startup validation runs via Bot.validate_system() before the trading loop.
- Graceful shutdown on Ctrl+C.
"""

from __future__ import annotations

import argparse
import asyncio
import signal as signal_mod
import sys

import structlog
import uvicorn

from polybot.config import load_config
from polybot.dashboard.app import app, set_bot
from polybot.main import Bot

logger = structlog.get_logger()


def _configure_logging(log_level: str) -> None:
    """Configure structlog to write to stdout AND the dashboard WebSocket terminal."""
    from polybot.dashboard.app import _broadcast_terminal, _terminal_buffer

    level = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}.get(
        log_level, 20
    )

    class DualOutputLogger:
        """Writes to stdout and pushes into the WebSocket terminal buffer.

        Accepts *args/**kwargs because structlog's logger_factory protocol may
        pass the logger name as a positional argument.
        """

        def __init__(self, *args, **kwargs) -> None:
            import sys as _sys
            self._file = _sys.stdout

        def msg(self, message: str) -> None:
            try:
                print(message, file=self._file)
            except Exception:
                pass
            try:
                s = str(message)
                _terminal_buffer.append(s)
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(_broadcast_terminal(s))
                except RuntimeError:
                    pass
            except Exception:
                pass

        debug = info = warning = error = critical = fatal = msg
        log = msg

        def __repr__(self) -> str:
            return "<DualOutputLogger>"

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=DualOutputLogger,
        cache_logger_on_first_use=False,
    )


def run_dashboard() -> None:
    """CLI entry point for the dashboard + bot."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="PolyBot Dashboard")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--mode", choices=["paper", "live"], help="Override trading mode")
    parser.add_argument("--host", default="0.0.0.0", help="Dashboard host")
    parser.add_argument("--port", type=int, default=8080, help="Dashboard port")
    parser.add_argument("--no-bot", action="store_true", help="Run dashboard only (no bot)")
    parser.add_argument(
        "--skip-validation", action="store_true",
        help="Skip startup validation (not recommended)",
    )
    args = parser.parse_args()

    # Load config first — bail early on bad YAML
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"\n[FATAL] {e}", file=sys.stderr)
        print("        Create a config.yaml in the project root or pass --config PATH\n",
              file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"\n[FATAL] Config load failed: {type(e).__name__}: {e}\n", file=sys.stderr)
        sys.exit(2)

    if args.mode:
        config.bot.mode = args.mode

    _configure_logging(config.bot.log_level)

    bot = Bot(config)
    set_bot(bot)

    async def run_all() -> None:
        # ── STARTUP VALIDATION ───────────────────────────────────────────
        if not args.no_bot and not args.skip_validation:
            ok, errors = await bot.validate_system()
            if not ok:
                print("\n" + "=" * 60, file=sys.stderr)
                print("  STARTUP VALIDATION FAILED", file=sys.stderr)
                print("=" * 60, file=sys.stderr)
                for err in errors:
                    print(f"  [x] {err}", file=sys.stderr)
                print("=" * 60, file=sys.stderr)
                print("  Fix the above issues and try again.", file=sys.stderr)
                print("  To bypass (not recommended): --skip-validation\n", file=sys.stderr)
                # Cleanly tear down any resources validate_system opened
                try:
                    await bot.stop()
                except Exception:
                    pass
                sys.exit(3)

        uvi_config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
        server = uvicorn.Server(uvi_config)

        shutdown_event = asyncio.Event()

        async def bot_runner() -> None:
            try:
                await bot.start()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("bot_runner_crashed", error=str(e), error_type=type(e).__name__)
            finally:
                shutdown_event.set()

        async def server_runner() -> None:
            try:
                await server.serve()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("server_crashed", error=str(e))
            finally:
                shutdown_event.set()

        tasks = [asyncio.create_task(server_runner())]
        if not args.no_bot:
            tasks.append(asyncio.create_task(bot_runner()))

        display_host = "localhost" if args.host == "0.0.0.0" else args.host
        print(f"\n{'=' * 60}")
        print("  POLYBOT DASHBOARD RUNNING")
        print(f"  URL: http://{display_host}:{args.port}")
        print(f"  Mode: {config.bot.mode.upper()}")
        print(f"  Strategies: {len(config.strategies.enabled)} loaded")
        print("  Terminal: Live log streaming via WebSocket")
        print("  Press Ctrl+C to stop")
        print(f"{'=' * 60}\n")

        try:
            import webbrowser
            webbrowser.open(f"http://{display_host}:{args.port}")
        except Exception:
            pass

        await shutdown_event.wait()

        logger.info("shutting_down")
        try:
            await bot.stop()
        except Exception as e:
            logger.error("bot_stop_error", error=str(e))

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal(sig: int, _frame) -> None:
        logger.info("signal_received", signal=sig)
        bot._running = False

    signal_mod.signal(signal_mod.SIGINT, handle_signal)
    try:
        signal_mod.signal(signal_mod.SIGTERM, handle_signal)
    except (ValueError, AttributeError):
        # SIGTERM may not be available on Windows for this signal handler flow
        pass

    try:
        loop.run_until_complete(run_all())
    except KeyboardInterrupt:
        try:
            loop.run_until_complete(bot.stop())
        except Exception:
            pass
    except SystemExit:
        raise
    except Exception as e:
        logger.error("launcher_fatal", error=str(e), error_type=type(e).__name__)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            try:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
        try:
            loop.close()
        except Exception:
            pass


if __name__ == "__main__":
    run_dashboard()
