from __future__ import annotations

"""Entry point — optimized for 5-minute BTC Up/Down latency arb.

Key fix: orderbook fetch errors now log at WARNING (not DEBUG) so you can
see when the CLOB API is failing to return data. Previously all ob_err
were silent DEBUG logs, hiding the root cause of zero-trade periods.
"""

from dotenv import load_dotenv
load_dotenv()

import asyncio
import signal
import sys
import time
from datetime import datetime

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

DEFAULT_PAPER_BALANCE = 500.0

# How often to log "no snapshot" warnings per market to avoid spam
_NO_SNAPSHOT_WARN_INTERVAL = 60.0  # seconds


class Bot:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._running = False
        self._wallet_balance = 0.0
        self._last_trade_time = 0.0
        self._orderbook_cache: dict[str, float] = {}
        self._no_snapshot_warn: dict[str, float] = {}  # market_id → last warn time
        self._ob_fail_count: dict[str, int] = {}       # market_id → consecutive fail count

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
        self._scanner = MarketScanner(self._client, config.scanner, self._event_bus)
        self._risk_manager = RiskManager(config.risk)
        self._circuit_breaker = CircuitBreaker(config.risk.circuit_breakers)
        self._position_manager = PositionManager(self._event_bus)
        self._execution_engine = ExecutionEngine(
            self._client, self._risk_manager, config.execution,
            self._event_bus, is_paper=not config.is_live,
        )
        self._alert_manager = AlertManager([LogChannel()])
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
    def config(self): return self._config
    @property
    def position_manager(self): return self._position_manager
    @property
    def circuit_breaker(self): return self._circuit_breaker
    @property
    def risk_manager(self): return self._risk_manager
    @property
    def execution_engine(self): return self._execution_engine
    @property
    def scanner(self): return self._scanner
    @property
    def exchange_feed(self): return self._exchange_feed
    @property
    def strategies(self): return self._strategies
    @property
    def running(self): return self._running
    @property
    def data_pipeline(self): return self._data_pipeline
    @property
    def storage(self): return self._storage

    async def start(self) -> None:
        logger.info("bot_starting", name=self._config.bot.name, mode=self._config.bot.mode)
        await self._storage.initialize()
        await self._client.start()
        await self._exchange_feed.start()

        if self._config.is_live:
            self._wallet_balance = await self._client.get_balance()
        else:
            self._wallet_balance = DEFAULT_PAPER_BALANCE
            logger.info("paper_balance_set", balance=self._wallet_balance)

        for sc in self._config.strategies.enabled:
            cls = STRATEGY_REGISTRY.get(sc.name)
            if cls:
                params = {k: v for k, v in sc.params.items() if k != "exchange_symbol"}
                strategy = cls(**params)
                if hasattr(strategy, "set_exchange_feed"):
                    feed = self._exchange_feed.get_feed(sc.params.get("exchange_symbol", "BTC"))
                    if feed:
                        strategy.set_exchange_feed(feed)
                self._strategies.append(strategy)
                logger.info("strategy_loaded", name=sc.name)

        self._event_bus.subscribe("market_discovered", self._on_market_discovered)
        self._event_bus.subscribe("order_filled", self._on_order_filled)
        await self._scanner.start()
        await self._ws_manager.start()
        self._running = True
        logger.info("bot_started", strategies=[s.name for s in self._strategies])
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
        if not order.strategy.startswith("exit_") and not order.strategy.startswith("auto_exit"):
            self._last_trade_time = time.time()

        if order.avg_fill_price > 0 and order.filled_size > 0:
            portfolio = self._position_manager.get_portfolio()
            self._circuit_breaker.record_trade_result(portfolio.daily_pnl)

        try:
            from polybot.dashboard.app import log_trade
            log_trade({
                "market_id": order.market_id, "side": order.side.value,
                "price": order.avg_fill_price, "size": order.filled_size,
                "strategy": order.strategy, "order_id": order.order_id,
            })
        except ImportError:
            pass

    def _market_time_remaining(self, market) -> float:
        now = datetime.utcnow()
        end = market.end_date.replace(tzinfo=None) if market.end_date.tzinfo else market.end_date
        return (end - now).total_seconds()

    async def _execute_exit(self, pos) -> None:
        """Execute a stop-loss exit. Bypasses trade interval cooldown."""
        close_side = Side.SELL if pos.side == Side.BUY else Side.BUY
        close_order = Order(
            market_id=pos.market_id, token_id=pos.token_id,
            side=close_side, price=pos.current_price, size=pos.size,
            order_type=OrderType.LIMIT, strategy=f"exit_{pos.strategy}",
        )
        saved_time = self._last_trade_time
        self._last_trade_time = 0
        portfolio = self._position_manager.get_portfolio()
        result = await self._execution_engine.execute_order(close_order, portfolio)
        self._last_trade_time = saved_time
        return result

    async def _fetch_orderbook_throttled(self, market_id: str, token_id: str) -> bool:
        now = time.time()
        if now - self._orderbook_cache.get(market_id, 0) < 1.0:
            return False
        try:
            ob = await self._client.get_orderbook(token_id)
            self._data_pipeline.ingest_orderbook(market_id, ob)
            self._orderbook_cache[market_id] = now
            self._ob_fail_count[market_id] = 0  # Reset on success
            return True
        except Exception as e:
            fails = self._ob_fail_count.get(market_id, 0) + 1
            self._ob_fail_count[market_id] = fails
            # Log at WARNING every 5th consecutive failure so it's visible
            if fails % 5 == 1:
                logger.warning(
                    "ob_fetch_fail",
                    m=market_id[:12],
                    consecutive_fails=fails,
                    error=str(e)[:80],
                )
            return False

    async def _trading_loop(self) -> None:
        cycle_count = 0

        while self._running:
            try:
                cycle_start = time.time()
                cycle_count += 1

                if not self._circuit_breaker.is_trading_allowed:
                    await asyncio.sleep(3)
                    continue

                if self._config.is_live and cycle_count % 30 == 0:
                    bal = await self._client.get_balance()
                    if bal > 0:
                        self._wallet_balance = bal

                min_interval = self._config.risk.min_trade_interval_seconds
                since_last = time.time() - self._last_trade_time
                in_cooldown = since_last < min_interval and self._last_trade_time > 0

                active_markets = self._scanner.active_markets
                sorted_markets = sorted(
                    active_markets.items(),
                    key=lambda x: self._market_time_remaining(x[1]),
                    reverse=True,
                )

                traded_this_cycle = False

                for market_id, market in sorted_markets:
                    time_left = self._market_time_remaining(market)

                    # Auto-close expiring positions (<30s)
                    if time_left < 30:
                        for p in list(self._position_manager.get_portfolio().positions):
                            if p.market_id == market_id:
                                logger.info(
                                    "auto_close",
                                    m=market_id[:12],
                                    t=round(time_left),
                                    pnl=round(p.unrealized_pnl, 4),
                                )
                                await self._execute_exit(p)
                        continue

                    if time_left < 60:
                        continue

                    if market.token_ids:
                        await self._fetch_orderbook_throttled(market_id, market.token_ids[0])

                    snapshot = self._data_pipeline.get_snapshot(market_id)
                    if not snapshot:
                        # Warn if we haven't had a snapshot for this market in a while
                        now = time.time()
                        last_warn = self._no_snapshot_warn.get(market_id, 0.0)
                        if now - last_warn > _NO_SNAPSHOT_WARN_INTERVAL:
                            self._no_snapshot_warn[market_id] = now
                            ob_fails = self._ob_fail_count.get(market_id, 0)
                            logger.warning(
                                "no_snapshot",
                                m=market_id[:12],
                                ob_fails=ob_fails,
                                hint="Check CLOB orderbook API or token_id validity",
                            )
                        continue

                    self._position_manager.update_prices(market_id, snapshot.orderbook.mid_price)

                    if in_cooldown or traded_this_cycle:
                        continue

                    signals = []
                    for strategy in self._strategies:
                        try:
                            sig = await strategy.evaluate(snapshot)
                            if sig:
                                signals.append(sig)
                                logger.info(
                                    "signal",
                                    s=sig.strategy,
                                    d=sig.direction.value,
                                    c=round(sig.confidence, 3),
                                    m=market_id[:12],
                                    t=round(time_left),
                                    r=sig.reason[:100],
                                )
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
                            logger.debug("strat_err", s=strategy.name, e=str(e))

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
                        filled = await self._execution_engine.execute_order(order, portfolio)
                        if filled.filled_size > 0:
                            traded_this_cycle = True
                        await self._storage.save_order({
                            "order_id": filled.order_id,
                            "market_id": filled.market_id,
                            "token_id": filled.token_id,
                            "side": filled.side.value,
                            "price": filled.price,
                            "size": filled.size,
                            "order_type": filled.order_type.value,
                            "status": filled.status.value,
                            "strategy": filled.strategy,
                            "signal_id": filled.signal_id,
                            "filled_size": filled.filled_size,
                            "avg_fill_price": filled.avg_fill_price,
                            "created_at": filled.created_at.isoformat(),
                            "updated_at": filled.created_at.isoformat(),
                        })

                # Stop-loss exits (bypass cooldown)
                exits = self._position_manager.check_exits()
                for exit_signal in exits:
                    pos = exit_signal.position
                    logger.info(
                        "exit_exec",
                        m=pos.market_id[:12],
                        reason=exit_signal.reason[:40],
                        pnl=round(pos.unrealized_pnl, 4),
                    )
                    await self._execute_exit(pos)

                if cycle_count % 10 == 0:
                    portfolio = self._position_manager.get_portfolio()
                    portfolio.balance = self._wallet_balance
                    await self._storage.save_pnl_snapshot({
                        "timestamp": asyncio.get_event_loop().time(),
                        "realized_pnl": portfolio.realized_pnl,
                        "unrealized_pnl": portfolio.unrealized_pnl,
                        "total_exposure": portfolio.total_exposure,
                        "num_positions": len(portfolio.positions),
                    })

                if cycle_count % 60 == 0:
                    btc_feed = self._exchange_feed.get_feed("BTC")
                    if btc_feed:
                        logger.info(
                            "feed_diag",
                            btc=round(btc_feed.last_price, 2),
                            tps=round(btc_feed.ticks_per_second, 1),
                            ticks=len(btc_feed.ticks),
                            micro_mom=round(btc_feed.micro_momentum() * 10000, 2),
                            markets=len(active_markets),
                            positions=len(self._position_manager.get_portfolio().positions),
                        )

                elapsed = time.time() - cycle_start
                await asyncio.sleep(max(1.0 - elapsed, 0.1))

            except Exception as e:
                logger.error("loop_error", error=str(e))
                self._circuit_breaker.record_api_error()
                await asyncio.sleep(3)

    async def _on_market_discovered(self, market) -> None:
        self._data_pipeline.register_market(market)
        for token_id in market.token_ids:
            await self._ws_manager.subscribe_market(token_id)
        logger.info("market_sub", id=market.id[:16], q=market.question[:50])


def cli() -> None:
    import argparse
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--mode", choices=["paper", "live"])
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

    def handle_signal(sig: int, _frame):
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
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


if __name__ == "__main__":
    cli()
