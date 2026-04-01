"""Launcher that runs the bot and dashboard together."""

from __future__ import annotations

import argparse
import asyncio
import signal as signal_mod

import structlog
import uvicorn

from polybot.config import load_config
from polybot.dashboard.app import set_bot, app
from polybot.main import Bot

logger = structlog.get_logger()


def run_dashboard() -> None:
    """CLI entry point for the dashboard."""
    # Load .env file if present
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # python-dotenv not installed, rely on real env vars

    parser = argparse.ArgumentParser(description="PolyBot Dashboard")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--mode", choices=["paper", "live"], help="Override trading mode")
    parser.add_argument("--host", default="0.0.0.0", help="Dashboard host")
    parser.add_argument("--port", type=int, default=8080, help="Dashboard port")
    parser.add_argument("--no-bot", action="store_true", help="Run dashboard only (no bot)")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.mode:
        config.bot.mode = args.mode

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}.get(
                config.bot.log_level, 20
            )
        ),
    )

    bot = Bot(config)
    set_bot(bot)

    async def run_all() -> None:
        uvi_config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
        )
        server = uvicorn.Server(uvi_config)

        shutdown_event = asyncio.Event()

        async def bot_runner() -> None:
            try:
                await bot.start()
            except asyncio.CancelledError:
                pass
            finally:
                shutdown_event.set()

        async def server_runner() -> None:
            try:
                await server.serve()
            except asyncio.CancelledError:
                pass
            finally:
                shutdown_event.set()

        tasks = [asyncio.create_task(server_runner())]

        if not args.no_bot:
            tasks.append(asyncio.create_task(bot_runner()))

        logger.info(
            "dashboard_started",
            url=f"http://{args.host}:{args.port}",
            bot_mode=config.bot.mode if not args.no_bot else "disabled",
        )

        # Print clear startup message
        print(f"\n{'='*60}")
        print(f"  POLYBOT DASHBOARD RUNNING")
        print(f"  URL: http://{args.host}:{args.port}")
        print(f"  Mode: {config.bot.mode.upper()}")
        print(f"  Strategies: {len(config.strategies.enabled)} loaded")
        print(f"  Terminal: Live log streaming via WebSocket")
        print(f"  Press Ctrl+C to stop")
        print(f"{'='*60}\n")

        # Auto-open browser
        import webbrowser
        try:
            webbrowser.open(f"http://{args.host}:{args.port}")
        except Exception:
            pass

        # Wait for shutdown signal
        await shutdown_event.wait()

        # Graceful cleanup
        logger.info("shutting_down")
        try:
            await bot.stop()
        except Exception:
            pass

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal(sig: int, _frame) -> None:
        logger.info("signal_received", signal=sig)
        # Thread-safe way to stop — set the bot's running flag
        bot._running = False

    signal_mod.signal(signal_mod.SIGINT, handle_signal)
    signal_mod.signal(signal_mod.SIGTERM, handle_signal)

    try:
        loop.run_until_complete(run_all())
    except KeyboardInterrupt:
        try:
            loop.run_until_complete(bot.stop())
        except Exception:
            pass
    finally:
        # Cancel remaining tasks
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


if __name__ == "__main__":
    run_dashboard()
