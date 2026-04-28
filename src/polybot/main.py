"""Entry point — BTC Up/Down trading bot.

PATCHED v2 — fixes from first run observation:
1. _execute_exit now detects market-expired (t_rem < 0) condition and uses
   force_close_paper() to write a fill directly to PositionManager rather than
   going through ExecutionEngine. This prevents the infinite auto_close loop
   when an order exceeds max_order_size.
2. _on_order_filled now updates maker_edge inventory tracker so subsequent
   maker_edge.evaluate() calls see the inventory.
3. auto_close loop emits "market_expired_force_close" once per market and
   stops retrying.

Major changes vs original (kept):
- Per-market cooldown
- Multi-trade per cycle
- 500ms loop
- Decision log integration
"""

from __future__ import annotations

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import asyncio
import os
import signal
import time
from datetime import datetime

import httpx
import structlog

from polybot.config import Config, load_config
from polybot.data.client import PolymarketClient
from polybot.data.exchange_feed import ExchangePriceFeed
from polybot.data.features_logger import FeaturesLogger
from polybot.data.models import Order, OrderType, Side, OrderStatus
from polybot.data.pipeline import DataPipeline
from polybot.data.storage import Storage
from polybot.data.websocket import WebSocketManager
from polybot.diagnostics import decision_log
from polybot.diagnostics.decision_log import BlockReason, emit as emit_decision
from polybot.events import EventBus
from polybot.execution.engine import ExecutionEngine
from polybot.health_monitor import HealthMonitor
from polybot.monitoring.alerts import AlertManager, LogChannel
from polybot.positions.manager import PositionManager
from polybot.risk.circuit_breaker import CircuitBreaker
from polybot.risk.manager import RiskManager
from polybot.scanner.scanner import MarketScanner
from polybot.strategies.aggregator import StrategyAggregator
from polybot.strategies.base import BaseStrategy
from polybot.strategies.boundary_decay import BoundaryDecayStrategy
from polybot.strategies.calibration_edge import CalibrationEdgeStrategy
from polybot.strategies.dual_direction_arb import DualDirectionArbStrategy
from polybot.strategies.maker_edge import MakerEdgeStrategy
from polybot.strategies.market_maker import MarketMakerStrategy
from polybot.strategies.mean_reversion import MeanReversionStrategy
from polybot.strategies.momentum import MomentumStrategy
from polybot.strategies.monte_carlo import MonteCarloStrategy
from polybot.strategies.overshoot_reversion import OvershootReversionStrategy
from polybot.strategies.volatility_breakout import VolatilityBreakoutStrategy

logger = structlog.get_logger()

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "overshoot_reversion": OvershootReversionStrategy,
    "boundary_decay": BoundaryDecayStrategy,
    "dual_direction_arb": DualDirectionArbStrategy,
    "maker_edge": MakerEdgeStrategy,
    "mean_reversion": MeanReversionStrategy,
    "momentum": MomentumStrategy,
    "volatility_breakout": VolatilityBreakoutStrategy,
    "market_maker": MarketMakerStrategy,
    "monte_carlo": MonteCarloStrategy,
    "calibration_edge": CalibrationEdgeStrategy,
}


def _lazy_import_killed(name: str) -> type[BaseStrategy] | None:
    if name == "latency_arb":
        from polybot.strategies.latency_arb import LatencyArbStrategy
        return LatencyArbStrategy
    if name == "momentum_lag":
        from polybot.strategies.momentum_lag import MomentumLagStrategy
        return MomentumLagStrategy
    return None


DEFAULT_PAPER_BALANCE = 500.0
_NO_SNAPSHOT_WARN_INTERVAL = 60.0
_SETTLEMENT_BACKFILL_INTERVAL = 60.0
_GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
_STARTUP_FEED_TIMEOUT_S = 12.0
_STARTUP_SCAN_TIMEOUT_S = 45.0


class Bot:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._running = False
        self._wallet_balance = 0.0
        self._last_trade_times: dict[tuple[str, str], float] = {}
        self._orderbook_cache: dict[str, float] = {}
        self._no_snapshot_warn: dict[str, float] = {}
        self._ob_fail_count: dict[str, int] = {}
        self._pending_settlement: dict[str, float] = {}
        self._backfilled: set[str] = set()
        # NEW: track which markets we've force-closed to prevent re-attempts
        self._force_closed_markets: set[str] = set()

        self._event_bus = EventBus()
        self._storage = Storage(f"{config.bot.data_dir}/bot.db")
        self._features_logger = FeaturesLogger(
            db_path=f"{config.bot.data_dir}/features.db",
            flush_interval_seconds=5.0,
            max_buffer_rows=200,
        )
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
            strategy_weights=config.strategies.aggregation.strategy_weights,
            min_net_score=config.strategies.aggregation.min_net_score,
        )
        self._backfill_client: httpx.AsyncClient | None = None
        self._backfill_task: asyncio.Task | None = None
        self._services_started = False

        decision_log.configure(
            path=config.decision_log.path,
            flush_every=config.decision_log.flush_every,
        )

        self._health = HealthMonitor()
        self._health.configure_threshold("binance_btc", 5.0)
        self._health.configure_threshold("polymarket_book", 30.0)
        self._health.configure_threshold("scanner", 120.0)
        self._stale_feed_warned: float = 0.0

    @staticmethod
    def _safe_env(env_var: str) -> str:
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
    @property
    def features_logger(self): return self._features_logger

    # ═════════════════════════════════════════════════════════════════
    #  STARTUP VALIDATION (unchanged)
    # ═════════════════════════════════════════════════════════════════

    async def validate_system(self) -> tuple[bool, list[str]]:
        errors: list[str] = []

        print("\n" + "=" * 60)
        print("  STARTUP VALIDATION")
        print("=" * 60)

        print("  [1/6] Config ............................ ", end="", flush=True)
        try:
            assert self._config is not None
            assert self._config.bot.mode in ("paper", "live")
            assert self._config.bot.data_dir
            if not self._config.strategies.enabled:
                raise AssertionError("no strategies enabled in config")
            print("OK")
        except Exception as e:
            print("FAIL")
            errors.append(f"Config invalid: {e}")
            return False, errors

        print("  [2/6] Storage (bot.db) .................. ", end="", flush=True)
        try:
            await self._storage.initialize()
            print("OK")
        except Exception as e:
            print("FAIL")
            errors.append(f"Storage: {type(e).__name__}: {e}")
            return False, errors

        print("  [3/6] Features DB ....................... ", end="", flush=True)
        try:
            await self._features_logger.start()
            unsettled = await self._features_logger.count_unsettled()
            print(f"OK (existing unsettled rows: {unsettled})")
        except Exception as e:
            print("FAIL")
            errors.append(f"Features: {type(e).__name__}: {e}")
            return False, errors

        print("  [4/6] Exchange feed ..................... ", end="", flush=True)
        try:
            await self._client.start()
            await self._exchange_feed.start()
            self._backfill_client = httpx.AsyncClient(timeout=10.0)

            btc = self._exchange_feed.get_feed("BTC")
            if btc is None:
                raise RuntimeError("BTC feed not created")

            deadline = time.time() + _STARTUP_FEED_TIMEOUT_S
            while time.time() < deadline:
                if btc.last_price > 0 and len(btc.ticks) >= 2:
                    break
                await asyncio.sleep(0.2)

            if btc.last_price <= 0:
                raise RuntimeError(
                    f"No BTC ticks in {_STARTUP_FEED_TIMEOUT_S}s"
                )
            print(f"OK (BTC=${btc.last_price:,.2f})")
        except Exception as e:
            print("FAIL")
            errors.append(f"Feed: {type(e).__name__}: {e}")
            return False, errors

        print("  [5/6] Strategies ........................ ", end="", flush=True)
        try:
            self._strategies = []
            for sc in self._config.strategies.enabled:
                cls = STRATEGY_REGISTRY.get(sc.name) or _lazy_import_killed(sc.name)
                if cls is None:
                    raise RuntimeError(f"Strategy {sc.name!r} not registered")
                params = {k: v for k, v in sc.params.items() if k != "exchange_symbol"}
                strategy = cls(**params)

                if hasattr(strategy, "set_exchange_feed"):
                    feed = self._exchange_feed.get_feed(sc.params.get("exchange_symbol", "BTC"))
                    if feed is not None:
                        strategy.set_exchange_feed(feed)

                self._strategies.append(strategy)

            names = ", ".join(s.name for s in self._strategies)
            print(f"OK ({len(self._strategies)}: {names})")
        except Exception as e:
            print("FAIL")
            errors.append(f"Strategies: {type(e).__name__}: {e}")
            return False, errors

        print("  [6/6] Market scan ....................... ", end="", flush=True)
        try:
            markets = await asyncio.wait_for(
                self._client.get_markets(active=True),
                timeout=_STARTUP_SCAN_TIMEOUT_S,
            )
            from polybot.scanner.scanner import classify_btc_market
            btc_count = sum(1 for m in markets if classify_btc_market(m.question))
            if btc_count == 0:
                raise RuntimeError(f"no BTC markets ({len(markets)} scanned)")
            print(f"OK ({btc_count} BTC markets)")
        except asyncio.TimeoutError:
            print("FAIL")
            errors.append(f"Scan timed out after {_STARTUP_SCAN_TIMEOUT_S}s")
            return False, errors
        except Exception as e:
            print("FAIL")
            errors.append(f"Scan: {type(e).__name__}: {e}")
            return False, errors

        self._services_started = True
        print("=" * 60)
        print("  VALIDATION PASSED — starting trading loop")
        print("=" * 60 + "\n")
        return True, []

    # ═════════════════════════════════════════════════════════════════
    #  LIFECYCLE
    # ═════════════════════════════════════════════════════════════════

    async def start(self) -> None:
        logger.info("bot_starting", name=self._config.bot.name, mode=self._config.bot.mode)

        if not self._services_started:
            await self._storage.initialize()
            await self._features_logger.start()
            await self._client.start()
            await self._exchange_feed.start()
            if self._backfill_client is None:
                self._backfill_client = httpx.AsyncClient(timeout=10.0)

            for sc in self._config.strategies.enabled:
                cls = STRATEGY_REGISTRY.get(sc.name) or _lazy_import_killed(sc.name)
                if not cls:
                    logger.warning("strategy_not_found", name=sc.name)
                    continue
                params = {k: v for k, v in sc.params.items() if k != "exchange_symbol"}
                try:
                    strategy = cls(**params)
                except TypeError as e:
                    logger.error("strategy_init_failed", name=sc.name, error=str(e))
                    continue
                if hasattr(strategy, "set_exchange_feed"):
                    feed = self._exchange_feed.get_feed(sc.params.get("exchange_symbol", "BTC"))
                    if feed:
                        strategy.set_exchange_feed(feed)
                self._strategies.append(strategy)
                logger.info("strategy_loaded", name=sc.name)

            self._services_started = True

        if self._config.is_live:
            try:
                self._wallet_balance = await self._client.get_balance()
            except Exception:
                self._wallet_balance = 0.0
        else:
            self._wallet_balance = DEFAULT_PAPER_BALANCE
            logger.info("paper_balance_set", balance=self._wallet_balance)

        self._event_bus.subscribe("market_discovered", self._on_market_discovered)
        self._event_bus.subscribe("market_removed", self._on_market_removed)
        self._event_bus.subscribe("order_filled", self._on_order_filled)

        await self._scanner.start()
        await self._ws_manager.start()

        self._backfill_task = asyncio.create_task(self._settlement_backfill_loop())

        self._running = True
        logger.info(
            "bot_started",
            strategies=[s.name for s in self._strategies],
            loop_interval_ms=self._config.execution.loop_interval_ms,
        )
        await self._trading_loop()

    async def stop(self) -> None:
        logger.info("bot_stopping")
        self._running = False
        try:
            await self._execution_engine.cancel_all()
        except Exception as e:
            logger.error("cancel_all_err", error=str(e))
        if self._backfill_task:
            self._backfill_task.cancel()
            try:
                await self._backfill_task
            except (asyncio.CancelledError, Exception):
                pass
        for coro, name in [
            (self._scanner.stop(), "scanner"),
            (self._ws_manager.stop(), "ws_manager"),
            (self._exchange_feed.stop(), "exchange_feed"),
            (self._client.close(), "client"),
            (self._features_logger.stop(), "features_logger"),
            (self._storage.close(), "storage"),
        ]:
            try:
                await coro
            except Exception as e:
                logger.error("stop_err", component=name, error=str(e))
        if self._backfill_client:
            try:
                await self._backfill_client.aclose()
            except Exception:
                pass

        try:
            decision_log.shutdown()
        except Exception:
            pass

        logger.info("bot_stopped")

    # ═════════════════════════════════════════════════════════════════
    #  EVENT HANDLERS
    # ═════════════════════════════════════════════════════════════════

    async def _on_order_filled(self, order: Order, **kwargs) -> None:
        try:
            self._position_manager.update_from_fill(order)

            # NEW: update maker_edge inventory tracker
            for strategy in self._strategies:
                if strategy.name == "maker_edge" and order.strategy == "maker_edge":
                    delta_shares = order.filled_size if order.side == Side.BUY else -order.filled_size
                    strategy.update_inventory(
                        market_id=order.market_id,
                        delta_shares=delta_shares,
                        fill_price=order.avg_fill_price,
                    )
                # Reset maker inventory on close
                if (strategy.name == "maker_edge" and
                    (order.strategy.startswith("exit_maker_edge") or
                     order.strategy.startswith("auto_exit_maker_edge"))):
                    strategy.reset_inventory(order.market_id)

            if not order.strategy.startswith("exit_") and not order.strategy.startswith("auto_exit"):
                key = (order.strategy, order.market_id)
                self._last_trade_times[key] = time.time()

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
                    "target_price": order.price,
                })
            except (ImportError, Exception) as e:
                logger.debug("dashboard_log_trade_err", error=str(e))
        except Exception as e:
            logger.error("on_order_filled_err", error=str(e), error_type=type(e).__name__)

    async def _on_market_discovered(self, market) -> None:
        try:
            self._data_pipeline.register_market(market)
            for token_id in market.token_ids:
                try:
                    await self._ws_manager.subscribe_market(token_id)
                except Exception as e:
                    logger.warning("ws_subscribe_err", token_id=token_id[:16], error=str(e))
            logger.info("market_sub", id=market.id[:16], q=market.question[:50])
        except Exception as e:
            logger.error("on_market_discovered_err", error=str(e))

    async def _on_market_removed(self, market_id: str, **kwargs) -> None:
        try:
            if market_id not in self._backfilled:
                self._pending_settlement[market_id] = time.time()
            self._data_pipeline.unregister_market(market_id)
        except Exception as e:
            logger.warning("on_market_removed_err", error=str(e))

    # ═════════════════════════════════════════════════════════════════
    #  HELPERS
    # ═════════════════════════════════════════════════════════════════

    def _market_time_remaining(self, market) -> float:
        try:
            now = datetime.utcnow()
            end = market.end_date
            if getattr(end, "tzinfo", None) is not None:
                end = end.replace(tzinfo=None)
            return (end - now).total_seconds()
        except Exception:
            return 0.0

    def _cooldown_ok(
        self,
        strategy: str,
        market_id: str,
        confidence: float,
    ) -> tuple[bool, str]:
        cfg = self._config
        cooldown_s = cfg.risk.min_trade_interval_seconds
        per_market = cfg.execution.cooldown_per_market

        if per_market:
            key = (strategy, market_id)
        else:
            key = ("__global__", "__global__")

        last = self._last_trade_times.get(key, 0.0)
        if last == 0.0:
            return True, "ok"

        elapsed = time.time() - last
        if elapsed >= cooldown_s:
            return True, "ok"

        if (
            elapsed >= cfg.execution.high_conf_override_after_s
            and confidence >= cfg.execution.high_conf_override_threshold
        ):
            return True, "high_conf_override"

        return False, f"cooldown ({elapsed:.1f}s < {cooldown_s}s)"

    async def _execute_exit(self, pos, market_expired: bool = False):
        """Exit a position.

        PATCHED v2: when market_expired is True, force-close as paper-fill
        directly rather than going through ExecutionEngine. This bypasses
        max_order_size and prevents the infinite auto_close loop.
        """
        try:
            close_side = Side.SELL if pos.side == Side.BUY else Side.BUY

            # Force-close path: write fill directly (paper mode only)
            if market_expired and self._execution_engine.is_paper:
                if pos.market_id in self._force_closed_markets:
                    return None
                self._force_closed_markets.add(pos.market_id)

                logger.info(
                    "market_expired_force_close",
                    m=pos.market_id[:12],
                    pnl=round(pos.unrealized_pnl, 4),
                    size=round(pos.size, 2),
                    strat=pos.strategy,
                )

                # Construct synthetic fill at current mid (or last entry price)
                close_price = pos.current_price if pos.current_price > 0 else pos.avg_entry_price
                close_order = Order(
                    market_id=pos.market_id,
                    token_id=pos.token_id,
                    side=close_side,
                    price=close_price,
                    size=pos.size,
                    order_type=OrderType.LIMIT,
                    strategy=f"auto_exit_{pos.strategy}_expired",
                    order_id=f"forceclose-{int(time.time() * 1000)}",
                    filled_size=pos.size,
                    avg_fill_price=close_price,
                    status=OrderStatus.FILLED,
                    created_at=datetime.utcnow(),
                )
                # Emit fill event so PositionManager closes the position
                await self._event_bus.emit("order_filled", order=close_order)

                # Persist to storage
                try:
                    await self._storage.save_order({
                        "order_id": close_order.order_id,
                        "market_id": close_order.market_id,
                        "token_id": close_order.token_id,
                        "side": close_order.side.value,
                        "price": close_order.price,
                        "size": close_order.size,
                        "order_type": close_order.order_type.value,
                        "status": close_order.status.value,
                        "strategy": close_order.strategy,
                        "signal_id": "",
                        "filled_size": close_order.filled_size,
                        "avg_fill_price": close_order.avg_fill_price,
                        "created_at": close_order.created_at.isoformat(),
                        "updated_at": close_order.created_at.isoformat(),
                    })
                except Exception as e:
                    logger.warning("force_close_save_err", error=str(e))

                return close_order

            # Normal exit path
            close_order = Order(
                market_id=pos.market_id,
                token_id=pos.token_id,
                side=close_side,
                price=pos.current_price if pos.current_price > 0 else pos.avg_entry_price,
                size=pos.size,
                order_type=OrderType.LIMIT,
                strategy=f"exit_{pos.strategy}",
            )
            portfolio = self._position_manager.get_portfolio()
            result = await self._execution_engine.execute_order(close_order, portfolio)
            return result
        except Exception as e:
            logger.error("execute_exit_err", market=pos.market_id[:16], error=str(e))
            return None

    async def _fetch_orderbook_throttled(self, market_id: str, token_id: str) -> bool:
        now = time.time()
        if now - self._orderbook_cache.get(market_id, 0) < 1.0:
            return False
        try:
            ob = await self._client.get_orderbook(token_id)
            self._data_pipeline.ingest_orderbook(market_id, ob)
            self._orderbook_cache[market_id] = now
            self._ob_fail_count[market_id] = 0
            self._health.stamp("polymarket_book")
            return True
        except Exception as e:
            fails = self._ob_fail_count.get(market_id, 0) + 1
            self._ob_fail_count[market_id] = fails
            if fails % 5 == 1:
                logger.warning("ob_fetch_fail", m=market_id[:12], consecutive_fails=fails, error=str(e)[:80])
            return False

    # ═════════════════════════════════════════════════════════════════
    #  TRADING LOOP — multi-trade per cycle, force-close at expiry
    # ═════════════════════════════════════════════════════════════════

    async def _trading_loop(self) -> None:
        cycle_count = 0
        loop_interval_s = self._config.execution.loop_interval_ms / 1000.0
        max_per_cycle = self._config.execution.max_trades_per_cycle
        max_per_market_per_cycle = self._config.execution.max_trades_per_market_per_cycle

        while self._running:
            try:
                cycle_start = time.time()
                cycle_count += 1

                if not self._circuit_breaker.is_trading_allowed:
                    await asyncio.sleep(3)
                    continue

                btc_feed_now = self._exchange_feed.get_feed("BTC")
                if btc_feed_now is not None and btc_feed_now.last_price > 0 and not btc_feed_now.is_stale:
                    self._health.stamp("binance_btc")
                if self._scanner.active_markets:
                    self._health.stamp("scanner")

                if self._health.stale("binance_btc"):
                    now_ts = time.time()
                    if now_ts - self._stale_feed_warned > 30:
                        self._stale_feed_warned = now_ts
                        logger.warning("feed_stale_skip_cycle", feed="binance_btc",
                                       age_s=round(self._health.age_s("binance_btc"), 1))
                    await asyncio.sleep(loop_interval_s)
                    continue

                active_markets = self._scanner.active_markets
                try:
                    sorted_markets = sorted(
                        active_markets.items(),
                        key=lambda x: self._market_time_remaining(x[1]),
                        reverse=True,
                    )
                except Exception:
                    sorted_markets = list(active_markets.items())

                btc_feed = self._exchange_feed.get_feed("BTC")

                all_candidates: list[tuple] = []
                auto_close_threshold = self._config.execution.auto_close_before_expiry_s

                for market_id, market in sorted_markets:
                    try:
                        time_left = self._market_time_remaining(market)

                        # AUTO-CLOSE PATH (PATCHED)
                        # If t_rem < auto_close_threshold, exit any open positions.
                        # If market is EXPIRED (t_rem < 0), use force-close path.
                        if time_left < auto_close_threshold:
                            market_expired = time_left < 0
                            try:
                                for p in list(self._position_manager.get_portfolio().positions):
                                    if p.market_id != market_id:
                                        continue
                                    if market_expired and market_id in self._force_closed_markets:
                                        continue
                                    logger.info(
                                        "auto_close" if not market_expired else "force_close",
                                        m=market_id[:12],
                                        t=round(time_left),
                                        pnl=round(p.unrealized_pnl, 4),
                                    )
                                    await self._execute_exit(p, market_expired=market_expired)
                            except Exception as e:
                                logger.warning("auto_close_err", m=market_id[:12], error=str(e))
                            continue

                        if time_left < 60:
                            continue

                        if market.token_ids:
                            await self._fetch_orderbook_throttled(market_id, market.token_ids[0])

                        snapshot = None
                        try:
                            snapshot = self._data_pipeline.get_snapshot(market_id)
                        except Exception:
                            pass

                        if not snapshot:
                            now = time.time()
                            last_warn = self._no_snapshot_warn.get(market_id, 0.0)
                            if now - last_warn > _NO_SNAPSHOT_WARN_INTERVAL:
                                self._no_snapshot_warn[market_id] = now
                                logger.warning("no_snapshot", m=market_id[:12])
                            continue

                        try:
                            self._position_manager.update_prices(
                                market_id, snapshot.orderbook.mid_price
                            )
                        except Exception:
                            pass

                        try:
                            await self._features_logger.log_snapshot(
                                market_id=market_id, snapshot=snapshot,
                                btc_feed=btc_feed, time_remaining=time_left,
                            )
                        except Exception:
                            pass

                        signals = []
                        for strategy in self._strategies:
                            try:
                                sig = await strategy.evaluate(snapshot)
                                if sig:
                                    signals.append(sig)
                            except Exception as e:
                                logger.debug("strat_err", s=getattr(strategy, "name", "?"), e=str(e))

                        try:
                            final_signals = self._aggregator.aggregate(signals)
                        except Exception:
                            final_signals = []

                        for sig in final_signals:
                            all_candidates.append((market, market_id, sig))

                    except Exception as e:
                        logger.error("market_cycle_err", m=market_id[:16], error=str(e))
                        continue

                # Rank and execute
                def _score(item) -> float:
                    sig = item[2]
                    edge = sig.metadata.get("edge_bps", 0.0) if sig.metadata else 0.0
                    return sig.confidence * max(edge, 1.0)

                all_candidates.sort(key=_score, reverse=True)

                seen_markets: set[str] = set()
                trades_placed = 0

                for market, market_id, sig in all_candidates:
                    if trades_placed >= max_per_cycle:
                        break
                    if max_per_market_per_cycle and market_id in seen_markets:
                        continue

                    ok, cd_reason = self._cooldown_ok(sig.strategy, market_id, sig.confidence)
                    if not ok:
                        continue

                    try:
                        portfolio = self._position_manager.get_portfolio()
                        balance = self._wallet_balance if self._wallet_balance > 0 else DEFAULT_PAPER_BALANCE

                        tf = self._scanner.get_timeframe(market_id) or ""
                        ok_open, open_reason = self._risk_manager.can_open_position(
                            portfolio, sig, timeframe=tf,
                        )
                        if not ok_open:
                            continue

                        sizing = self._risk_manager.kelly_size_for_signal(
                            sig, bankroll=balance, timeframe=tf,
                        )
                        if sizing.size_usd <= 0:
                            continue

                        share_size = sizing.size_usd / max(sig.target_price, 0.01)

                        order_type = (
                            OrderType.GTC
                            if sig.metadata.get("is_maker_only")
                            or sig.metadata.get("is_market_maker")
                            else OrderType.LIMIT
                        )
                        order = Order(
                            market_id=sig.market_id,
                            token_id=market.token_ids[0] if market.token_ids else "",
                            side=Side.BUY if sig.direction.value == "BUY" else Side.SELL,
                            price=sig.target_price,
                            size=share_size,
                            order_type=order_type,
                            strategy=sig.strategy,
                        )
                        order.size *= self._circuit_breaker.size_multiplier

                        filled = await self._execution_engine.execute_order(order, portfolio)
                        if filled.filled_size > 0:
                            trades_placed += 1
                            seen_markets.add(market_id)

                        try:
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
                        except Exception:
                            pass
                    except Exception as e:
                        logger.error("signal_execution_err", error=str(e))

                # Stop-loss / strategy-specific exits
                try:
                    exits = self._position_manager.check_exits()
                except Exception:
                    exits = []

                for exit_signal in exits:
                    try:
                        pos = exit_signal.position
                        logger.info(
                            "exit_exec",
                            m=pos.market_id[:12],
                            reason=exit_signal.reason[:60],
                            pnl=round(pos.unrealized_pnl, 4),
                        )
                        await self._execute_exit(pos, market_expired=False)
                    except Exception as e:
                        logger.error("exit_exec_err", error=str(e))

                if cycle_count % 10 == 0:
                    try:
                        portfolio = self._position_manager.get_portfolio()
                        portfolio.balance = self._wallet_balance
                        await self._storage.save_pnl_snapshot({
                            "timestamp": datetime.utcnow().isoformat(),
                            "realized_pnl": portfolio.realized_pnl,
                            "unrealized_pnl": portfolio.unrealized_pnl,
                            "total_exposure": portfolio.total_exposure,
                            "num_positions": len(portfolio.positions),
                        })
                    except Exception:
                        pass

                if cycle_count % 60 == 0:
                    if btc_feed:
                        try:
                            from polybot.diagnostics.decision_log import block_summary
                            top_blocks = block_summary(top_n=8)
                            logger.info(
                                "feed_diag",
                                btc=round(btc_feed.last_price, 2),
                                tps=round(btc_feed.ticks_per_second, 1),
                                ticks=len(btc_feed.ticks),
                                markets=len(active_markets),
                                positions=len(self._position_manager.get_portfolio().positions),
                                feat_rows=self._features_logger.rows_written,
                                ws_books=self._ws_manager.book_events_received,
                                top_blocks=top_blocks,
                            )
                        except Exception:
                            pass

                elapsed = time.time() - cycle_start
                await asyncio.sleep(max(loop_interval_s - elapsed, 0.05))

            except Exception as e:
                logger.error("loop_error", error=str(e), error_type=type(e).__name__)
                try:
                    self._circuit_breaker.record_api_error()
                except Exception:
                    pass
                await asyncio.sleep(3)

    # ═════════════════════════════════════════════════════════════════
    #  SETTLEMENT BACKFILL (unchanged)
    # ═════════════════════════════════════════════════════════════════

    async def _settlement_backfill_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(_SETTLEMENT_BACKFILL_INTERVAL)
                if not self._pending_settlement:
                    continue

                now = time.time()
                ready = [
                    mid for mid, ts in list(self._pending_settlement.items())
                    if now - ts >= 30.0
                ]
                for market_id in ready:
                    try:
                        settled = await self._query_market_settlement(market_id)
                        if settled is not None:
                            await self._features_logger.backfill_settlement(market_id, settled)
                            self._backfilled.add(market_id)
                            self._pending_settlement.pop(market_id, None)
                        else:
                            if now - self._pending_settlement.get(market_id, now) > 600.0:
                                self._pending_settlement.pop(market_id, None)
                    except Exception:
                        pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("backfill_loop_err", error=str(e))

    async def _query_market_settlement(self, market_id: str) -> int | None:
        if self._backfill_client is None:
            return None
        try:
            resp = await self._backfill_client.get(
                _GAMMA_MARKETS_URL,
                params={"condition_ids": market_id, "closed": "true"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not item.get("closed"):
                    continue
                outcome_prices_raw = item.get("outcomePrices") or item.get("outcome_prices")
                if not outcome_prices_raw:
                    continue
                if isinstance(outcome_prices_raw, str):
                    import json
                    try:
                        outcome_prices = json.loads(outcome_prices_raw)
                    except Exception:
                        continue
                else:
                    outcome_prices = outcome_prices_raw
                if not isinstance(outcome_prices, (list, tuple)) or not outcome_prices:
                    continue
                try:
                    yes_price = float(outcome_prices[0])
                except (TypeError, ValueError):
                    continue
                return 1 if yes_price > 0.5 else 0
        except Exception:
            pass
        return None


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
    parser.add_argument("--skip-validation", action="store_true")
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
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except (ValueError, AttributeError):
        pass

    async def main_async():
        if not args.skip_validation:
            ok, errors = await bot.validate_system()
            if not ok:
                for err in errors:
                    print(f"[x] {err}")
                try:
                    await bot.stop()
                except Exception:
                    pass
                return
        await bot.start()

    try:
        loop.run_until_complete(main_async())
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
            try:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
        loop.close()


if __name__ == "__main__":
    cli()
