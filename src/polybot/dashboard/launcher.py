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
    parser.add_argument("--host", default="127.0.0.1", help="Dashboard host")
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
        # Start uvicorn in the background
        uvi_config = uvicorn.Config(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
        )
        server = uvicorn.Server(uvi_config)

        tasks = [asyncio.create_task(server.serve())]

        if not args.no_bot:
            tasks.append(asyncio.create_task(bot.start()))

        logger.info(
            "dashboard_started",
            url=f"http://{args.host}:{args.port}",
            bot_mode=config.bot.mode if not args.no_bot else "disabled",
        )

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

    loop = asyncio.new_event_loop()

    def handle_signal(sig: int, _frame) -> None:
        logger.info("signal_received", signal=sig)
        loop.create_task(bot.stop())

    signal_mod.signal(signal_mod.SIGINT, handle_signal)
    signal_mod.signal(signal_mod.SIGTERM, handle_signal)

    try:
        loop.run_until_complete(run_all())
    except KeyboardInterrupt:
        loop.run_until_complete(bot.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    run_dashboard()
