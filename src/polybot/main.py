"""Entry point and orchestrator for the Polymarket trading bot."""

from __future__ import annotations

import asyncio
import signal
import sys

import structlog

from polybot.config import load_config, Config
from polybot.data.client import PolymarketClient
from polybot.data.exchange_feed import ExchangePriceFeed
from polybot.data.pipeline import DataPipeline
from polybot.data.storage import Storage
from polybot.data.websocket import WebSocketManager
from polybot.events import EventBus
from polybot.execution.engine import ExecutionEngine
from polybot.monitoring.alerts import AlertManager, LogChannel
from polybot.positions.manager import PositionManager
from polybot.risk.circuit_breaker import CircuitBreaker
from polybot.risk.manager import RiskManager
from polybot.scanner.scanner import MarketScanner
from polybot.strategies.aggregator import StrategyAggregator
from polybot.strategies.base import BaseStrategy
from polybot.strategies.mean_reversion import MeanReversionStrategy
from polybot.strategies.momentum import MomentumStrategy
from polybot.strategies.latency_arb import LatencyArbStrategy
from polybot.strategies.momentum_lag import MomentumLagStrategy
from polybot.strategies.volatility_breakout import VolatilityBreakoutStrategy
from polybot.strategies.dual_direction_arb import DualDirectionArbStrategy
from polybot.strategies.market_maker import MarketMakerStrategy
from polybot.strategies.monte_carlo import MonteCarloStrategy
from polybot.data.models import Order, OrderType, Side

logger = structlog.get_logger()

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "mean_reversion": MeanReversionStrategy,
    "momentum": MomentumStrategy,
    "latency_arb": LatencyArbStrategy,
    "momentum_lag": MomentumLagStrategy,
    "volatility_breakout": VolatilityBreakoutStrategy,
    "dual_direction_arb": DualDirectionArbStrategy,
    "market_maker": MarketMakerStrategy,
    "monte_carlo": MonteCarloStrategy,
}


class Bot:
    """Main bot orchestrator — wires components and runs the trading loop."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._running = False

        # Core infrastructure
        self._event_bus = EventBus()
        self._storage = Storage(f"{config.bot.data_dir}/bot.db")
        self._client = PolymarketClient(api_key="")  # Set in start()
        self._ws_manager = WebSocketManager(self._event_bus)
        self._data_pipeline = DataPipeline()
        self._exchange_feed = ExchangePriceFeed(
            symbols=["BTC", "ETH", "SOL", "XRP"],
            poll_interval=0.5,
        )

        # Trading components
        self._scanner = MarketScanner(self._client, config.scanner, self._event_bus)
        self._risk_manager = RiskManager(config.risk)
        self._circuit_breaker = CircuitBreaker(config.risk.circuit_breakers)
        self._position_manager = PositionManager(self._event_bus)
        self._execution_engine = ExecutionEngine(
            self._client,
            self._risk_manager,
            config.execution,
            self._event_bus,
            is_paper=not config.is_live,
        )
        self._alert_manager = AlertManager([LogChannel()])

        # Strategies
        self._strategies: list[BaseStrategy] = []
        self._aggregator = StrategyAggregator(
            min_confidence=config.strategies.aggregation.min_confidence,
            conflict_resolution=config.strategies.aggregation.conflict_resolution,
        )

    @property
    def config(self) -> Config:
        return self._config

    @property
    def position_manager(self) -> PositionManager:
        return self._position_manager

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    @property
    def risk_manager(self) -> RiskManager:
        return self._risk_manager

    @property
    def execution_engine(self) -> ExecutionEngine:
        return self._execution_engine

    @property
    def scanner(self) -> MarketScanner:
        return self._scanner

    @property
    def exchange_feed(self) -> ExchangePriceFeed:
        return self._exchange_feed

    @property
    def strategies(self) -> list[BaseStrategy]:
        return self._strategies

    @property
    def running(self) -> bool:
        return self._running

    @property
    def data_pipeline(self) -> DataPipeline:
        return self._data_pipeline

    @property
    def storage(self) -> Storage:
        return self._storage

    async def start(self) -> None:
        """Initialize all components and start the trading loop."""
        logger.info(
            "bot_starting",
            name=self._config.bot.name,
            mode=self._config.bot.mode,
        )

        # Initialize
        await self._storage.initialize()
        await self._client.start()
        await self._exchange_feed.start()

        # Load strategies
        for strat_config in self._config.strategies.enabled:
            cls = STRATEGY_REGISTRY.get(strat_config.name)
            if cls:
                strategy = cls(**strat_config.params)
                # Wire exchange feed into strategies that need it
                if hasattr(strategy, "set_exchange_feed"):
                    # Default to BTC feed; can be configured per-strategy
                    symbol = strat_config.params.get("exchange_symbol", "BTC")
                    feed = self._exchange_feed.get_feed(symbol)
                    if feed:
                        strategy.set_exchange_feed(feed)
                self._strategies.append(strategy)
                logger.info("strategy_loaded", name=strat_config.name)
            else:
                logger.warning("strategy_unknown", name=strat_config.name)

        # Wire events
        self._event_bus.subscribe("market_discovered", self._on_market_discovered)

        # Start components
        await self._scanner.start()
        await self._ws_manager.start()

        self._running = True
        logger.info("bot_started")

        # Run trading loop
        await self._trading_loop()

    async def stop(self) -> None:
        """Gracefully shut down."""
        logger.info("bot_stopping")
        self._running = False
        await self._execution_engine.cancel_all()
        await self._scanner.stop()
        await self._ws_manager.stop()
        await self._exchange_feed.stop()
        await self._client.close()
        await self._storage.close()
        logger.info("bot_stopped")

    async def _trading_loop(self) -> None:
        """Main loop: evaluate strategies and execute signals."""
        while self._running:
            try:
                if not self._circuit_breaker.is_trading_allowed:
                    logger.debug("trading_paused_circuit_breaker")
                    await asyncio.sleep(10)
                    continue

                # Evaluate all active markets
                for market_id in self._scanner.active_markets:
                    snapshot = self._data_pipeline.get_snapshot(market_id)
                    if not snapshot:
                        continue

                    # Update position prices
                    self._position_manager.update_prices(
                        market_id, snapshot.orderbook.mid_price
                    )

                    # Generate signals from all strategies
                    signals = []
                    for strategy in self._strategies:
                        signal = await strategy.evaluate(snapshot)
                        if signal:
                            signals.append(signal)

                    # Aggregate and execute
                    final_signals = self._aggregator.aggregate(signals)
                    for sig in final_signals:
                        portfolio = self._position_manager.get_portfolio()
                        order = Order(
                            market_id=sig.market_id,
                            token_id=snapshot.market.token_ids[0]
                            if snapshot.market.token_ids
                            else "",
                            side=Side.BUY if sig.direction.value == "BUY" else Side.SELL,
                            price=sig.target_price,
                            size=sig.size_pct * portfolio.balance if portfolio.balance > 0 else 100,
                            order_type=OrderType.LIMIT,
                            strategy=sig.strategy,
                        )
                        order.size *= self._circuit_breaker.size_multiplier
                        await self._execution_engine.execute_order(order, portfolio)

                # Check exits
                exits = self._position_manager.check_exits()
                for exit_signal in exits:
                    logger.info(
                        "exit_triggered",
                        market=exit_signal.position.market_id,
                        reason=exit_signal.reason,
                    )

                await asyncio.sleep(5)

            except Exception as e:
                logger.error("trading_loop_error", error=str(e))
                self._circuit_breaker.record_api_error()
                await asyncio.sleep(10)

    async def _on_market_discovered(self, market) -> None:
        self._data_pipeline.register_market(market)
        for token_id in market.token_ids:
            await self._ws_manager.subscribe_market(token_id)
        logger.info("market_subscribed", market_id=market.id, question=market.question)


def cli() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--mode", choices=["paper", "live"], help="Override trading mode")
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

    loop = asyncio.new_event_loop()

    def handle_signal(sig: int, _frame) -> None:
        logger.info("signal_received", signal=sig)
        loop.create_task(bot.stop())

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        loop.run_until_complete(bot.stop())
    finally:
        loop.close()


if __name__ == "__main__":
    cli()
