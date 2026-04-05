from __future__ import annotations

"""Entry point and orchestrator for the Polymarket trading bot."""

from dotenv import load_dotenv
load_dotenv()

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
from polybot.strategies.calibration_edge import CalibrationEdgeStrategy
from polybot.strategies.maker_edge import MakerEdgeStrategy
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
    "calibration_edge": CalibrationEdgeStrategy,
    "maker_edge": MakerEdgeStrategy,
}

DEFAULT_PAPER_BALANCE = 10_000.0


class Bot:
    """Main bot orchestrator — wires components and runs the trading loop."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._running = False
        self._wallet_balance = 0.0

        # Core infrastructure
        self._event_bus = EventBus()
        self._storage = Storage(f"{config.bot.data_dir}/bot.db")
        self._client = PolymarketClient(
            api_key=self._safe_env(config.wallet.api_key_env),
            private_key=self._safe_env(config.wallet.private_key_env),
        )
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

    @staticmethod
    def _safe_env(env_var: str) -> str:
        import os
        return os.environ.get(env_var, "")

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
        logger.info(
            "bot_starting",
            name=self._config.bot.name,
            mode=self._config.bot.mode,
        )

        await self._storage.initialize()
        await self._client.start()
        await self._exchange_feed.start()

        if self._config.is_live:
            self._wallet_balance = await self._client.get_balance()
            logger.info("wallet_balance_fetched", balance=self._wallet_balance)
        else:
            self._wallet_balance = DEFAULT_PAPER_BALANCE
            logger.info("paper_balance_set", balance=self._wallet_balance)

        # Load strategies
        for strat_config in self._config.strategies.enabled:
            cls = STRATEGY_REGISTRY.get(strat_config.name)
            if cls:
                ctor_params = {
                    k: v for k, v in strat_config.params.items()
                    if k != "exchange_symbol"
                }
                strategy = cls(**ctor_params)
                if hasattr(strategy, "set_exchange_feed"):
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
        self._event_bus.subscribe("order_filled", self._on_order_filled)

        # Start components
        await self._scanner.start()
        await self._ws_manager.start()

        self._running = True
        logger.info("bot_started")

        await self._trading_loop()

    async def stop(self) -> None:
        logger.info("bot_stopping")
        self._running = False
        await self._execution_engine.cancel_all()
        await self._scanner.stop()
        await self._ws_manager.stop()
        await self._exchange_feed.stop()
        await self._client.close()
        await self._storage.close()
        logger.info("bot_stopped")

    async def _on_order_filled(self, order: Order, **kwargs) -> None:
        self._position_manager.update_from_fill(order)

        if order.avg_fill_price > 0 and order.filled_size > 0:
            portfolio = self._position_manager.get_portfolio()
            self._circuit_breaker.record_trade_result(portfolio.daily_pnl)

        try:
            from polybot.dashboard.app import log_trade
            log_trade({
                "market_id": order.market_id,
                "side": order.side.value,
                "price": order.avg_fill_price,
                "size": order.filled_size,
                "strategy": order.strategy,
                "order_id": order.order_id,
            })
        except ImportError:
            pass

    async def _trading_loop(self) -> None:
        """Main loop: fetch orderbooks, evaluate strategies, execute signals."""
        while self._running:
            try:
                if not self._circuit_breaker.is_trading_allowed:
                    logger.debug("trading_paused_circuit_breaker")
                    await asyncio.sleep(10)
                    continue

                if self._config.is_live:
                    balance = await self._client.get_balance()
                    if balance > 0:
                        self._wallet_balance = balance

                # Evaluate all active markets
                active_markets = self._scanner.active_markets
                for market_id, market in active_markets.items():

                    # ═══ POLL ORDERBOOK VIA REST ═══
                    # The WebSocket isn't feeding data yet, so we fetch
                    # orderbooks directly for each market every loop cycle.
                    if market.token_ids:
                        try:
                            orderbook = await self._client.get_orderbook(market.token_ids[0])
                            self._data_pipeline.ingest_orderbook(market_id, orderbook)
                        except Exception as e:
                            logger.debug("orderbook_fetch_error", market=market_id[:16], error=str(e))

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
                        try:
                            sig = await strategy.evaluate(snapshot)
                            if sig:
                                signals.append(sig)
                                try:
                                    from polybot.dashboard.app import log_signal
                                    log_signal({
                                        "market_id": sig.market_id,
                                        "strategy": sig.strategy,
                                        "direction": sig.direction.value,
                                        "confidence": round(sig.confidence, 3),
                                        "reason": sig.reason,
                                        "target_price": round(sig.target_price, 4),
                                        "size_pct": round(sig.size_pct, 4),
                                    })
                                except ImportError:
                                    pass
                        except Exception as e:
                            logger.debug("strategy_error", strategy=strategy.name, error=str(e))

                    # Aggregate and execute
                    final_signals = self._aggregator.aggregate(signals)
                    for sig in final_signals:
                        portfolio = self._position_manager.get_portfolio()
                        balance = self._wallet_balance if self._wallet_balance > 0 else DEFAULT_PAPER_BALANCE
                        order_type = (
                            OrderType.GTC
                            if sig.metadata.get("is_maker_only") or sig.metadata.get("is_market_maker")
                            else OrderType.LIMIT
                        )
                        order = Order(
                            market_id=sig.market_id,
                            token_id=market.token_ids[0] if market.token_ids else "",
                            side=Side.BUY if sig.direction.value == "BUY" else Side.SELL,
                            price=sig.target_price,
                            size=sig.size_pct * balance,
                            order_type=order_type,
                            strategy=sig.strategy,
                        )
                        order.size *= self._circuit_breaker.size_multiplier
                        filled_order = await self._execution_engine.execute_order(order, portfolio)

                        await self._storage.save_order({
                            "order_id": filled_order.order_id,
                            "market_id": filled_order.market_id,
                            "token_id": filled_order.token_id,
                            "side": filled_order.side.value,
                            "price": filled_order.price,
                            "size": filled_order.size,
                            "order_type": filled_order.order_type.value,
                            "status": filled_order.status.value,
                            "strategy": filled_order.strategy,
                            "signal_id": filled_order.signal_id,
                            "filled_size": filled_order.filled_size,
                            "avg_fill_price": filled_order.avg_fill_price,
                            "created_at": filled_order.created_at.isoformat(),
                            "updated_at": filled_order.created_at.isoformat(),
                        })

                # Check exits
                exits = self._position_manager.check_exits()
                for exit_signal in exits:
                    logger.info(
                        "exit_triggered",
                        market=exit_signal.position.market_id,
                        reason=exit_signal.reason,
                    )

                # Snapshot P&L
                portfolio = self._position_manager.get_portfolio()
                portfolio.balance = self._wallet_balance
                await self._storage.save_pnl_snapshot({
                    "timestamp": asyncio.get_event_loop().time(),
                    "realized_pnl": portfolio.realized_pnl,
                    "unrealized_pnl": portfolio.unrealized_pnl,
                    "total_exposure": portfolio.total_exposure,
                    "num_positions": len(portfolio.positions),
                })

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
    import argparse

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

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
        bot._running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        loop.run_until_complete(bot.start())
    except KeyboardInterrupt:
        pass
    finally:
        if bot._running:
            bot._running = False
        try:
            loop.run_until_complete(bot.stop())
        except Exception:
            pass
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


if __name__ == "__main__":
    cli()
