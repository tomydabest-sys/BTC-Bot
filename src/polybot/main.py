"""Bot orchestrator — wires components, runs trading loop, handles fills/exits.

PATCHED v4:
1. sig.metadata is now copied into order.metadata when constructing orders
   (previously lost — Order had no metadata field)
2. Paper-mode bankroll comes from config.risk.bankroll_usd (was hardcoded 500)
3. Live mode is hard-guarded at startup — refuses to start unless
   config.bot.allow_live=True AND data.client.LIVE_TRADING_ENABLED=True
4. _force_closed_markets is now {market_id: expiry_ts} dict, pruned each cycle
5. AlertManager is wired into CircuitBreaker via set_alert_manager()
6. features.db cleanup task runs on FeaturesRetentionConfig schedule
7. Graceful shutdown drain — waits up to 5s for in-flight ops to finalise
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from datetime import UTC, datetime, timedelta

import structlog

from polybot.config import Config, load_config
from polybot.data.client import (
    LIVE_TRADING_ENABLED,
    LiveTradingNotImplementedError,
    PolymarketClient,
)
from polybot.data.exchange_feed import ExchangeFeed
from polybot.data.features_logger import FeaturesLogger
from polybot.data.models import (
    Direction,
    Market,
    MarketSnapshot,
    Order,
    OrderType,
    Side,
    Signal,
)
from polybot.data.pipeline import DataPipeline
from polybot.data.storage import Storage
from polybot.data.websocket import WebSocketManager
from polybot.diagnostics.decision_log import (
    BlockReason,
    block_summary,
)
from polybot.diagnostics.decision_log import (
    configure as configure_decision_log,
)
from polybot.diagnostics.decision_log import (
    emit as emit_decision,
)
from polybot.diagnostics.decision_log import (
    shutdown as shutdown_decision_log,
)
from polybot.events import EventBus
from polybot.execution.engine import ExecutionEngine
from polybot.execution.maker_orchestrator import MakerOrchestrator
from polybot.health_monitor import get_monitor
from polybot.monitoring.alerts import AlertManager
from polybot.monitoring.telegram_alerts import TelegramAlerter
from polybot.positions.manager import PositionManager
from polybot.risk.circuit_breaker import CircuitBreaker
from polybot.risk.manager import RiskManager
from polybot.scanner.scanner import MarketScanner
from polybot.strategies.aggregator import StrategyAggregator
from polybot.strategies.base import BaseStrategy
from polybot.strategies.boundary_decay import BoundaryDecayStrategy
from polybot.strategies.dual_direction_arb import DualDirectionArbStrategy
from polybot.strategies.maker_edge import MakerEdgeStrategy
from polybot.strategies.overshoot_reversion import OvershootReversionStrategy

logger = structlog.get_logger()


STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "overshoot_reversion": OvershootReversionStrategy,
    "boundary_decay": BoundaryDecayStrategy,
    "dual_direction_arb": DualDirectionArbStrategy,
    "maker_edge": MakerEdgeStrategy,
}


# How long to keep a force-closed market_id in the dedupe set (seconds).
# Once the underlying market resolves and is gone from the scanner, anything
# longer than this is just memory waste.
_FORCE_CLOSED_TTL_S = 24 * 3600


# Drain window on graceful shutdown — wait for in-flight ops to ack
_SHUTDOWN_DRAIN_S = 5.0


class Bot:
    """Top-level orchestrator. Wires every component, runs the cycle loop."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._is_paper = config.bot.mode != "live"
        self._running = False

        # Live-mode hard guard — fail fast at construction time if misconfigured
        if not self._is_paper:
            if not config.bot.allow_live:
                raise RuntimeError(
                    "Live mode requested but config.bot.allow_live is False. "
                    "Set allow_live: true in your config to bypass this guard."
                )
            if not LIVE_TRADING_ENABLED:
                raise RuntimeError(
                    "Live mode requested but LIVE_TRADING_ENABLED is False in "
                    "polybot.data.client. EIP-712 order signing must be "
                    "implemented before live mode can be used."
                )
            logger.warning(
                "live_mode_starting",
                note="all hard guards passed; real money will be at risk",
            )

        # Configure decision log
        configure_decision_log(
            path=config.decision_log.path,
            flush_every=config.decision_log.flush_every,
        )

        # Core wiring
        self._event_bus = EventBus()
        self._client = PolymarketClient(
            api_key=os.environ.get(config.wallet.api_key_env, ""),
            private_key=os.environ.get(config.wallet.private_key_env, ""),
        )
        self._ws = WebSocketManager(self._event_bus)
        self._scanner = MarketScanner(self._client, config.scanner, self._event_bus)
        self._pipeline = DataPipeline(self._client, self._ws, self._event_bus)

        self._exchange_feed = ExchangeFeed(symbol="BTC")
        self._risk = RiskManager(config.risk)
        self._circuit_breaker = CircuitBreaker(config.risk.circuit_breakers)
        self._positions = PositionManager(
            self._event_bus,
            max_hold_seconds=config.execution.max_position_hold_seconds,
        )
        self._execution = ExecutionEngine(
            self._client,
            self._risk,
            config.execution,
            self._event_bus,
            is_paper=self._is_paper,
        )
        self._alerts = AlertManager(config.monitoring.alerts)
        self._circuit_breaker.set_alert_manager(self._alerts)

        # Maker-mode subsystem (V2). Lazily built only when enabled so the
        # existing taker-mode bot continues to run without the new wiring.
        self._maker: MakerOrchestrator | None = None
        self._telegram: TelegramAlerter | None = None
        if config.maker.enabled:
            self._telegram = TelegramAlerter(
                self._alerts,
                heartbeat_interval_s=config.deployment.heartbeat_interval_s,
            )
            self._maker = MakerOrchestrator(
                maker_cfg=config.maker,
                scanner=self._scanner,
                pipeline=self._pipeline,
                exchange_feed=self._exchange_feed,
                is_paper=self._is_paper,
                client=None,  # paper mode; live wiring lands with EIP-712
                telegram=self._telegram,
            )

        self._storage = Storage(db_path=os.path.join(config.bot.data_dir, "bot.db"))
        self._features = FeaturesLogger(
            db_path=os.path.join(config.bot.data_dir, "features.db"),
        )

        # Strategies — built from config
        self._strategies = self._build_strategies()
        for s in self._strategies:
            if hasattr(s, "set_exchange_feed"):
                s.set_exchange_feed(self._exchange_feed.state)

        self._aggregator = StrategyAggregator(
            min_confidence=config.strategies.aggregation.min_confidence,
            conflict_resolution=config.strategies.aggregation.conflict_resolution,
            strategy_weights=config.strategies.aggregation.strategy_weights,
            min_net_score=config.strategies.aggregation.min_net_score,
        )

        # Trading-loop state
        # Map market_id -> expiry timestamp; pruned each cycle
        self._force_closed_markets: dict[str, float] = {}
        self._market_cooldown: dict[str, float] = {}
        self._last_signal_ts: dict[str, float] = {}
        self._cycle_count: int = 0
        self._tasks: list[asyncio.Task] = []
        # Halt diagnostic: tracks how long the position cap has been pegged
        # so we can surface "bot is throttled because all slots are stuck"
        # in the logs instead of looking like a silent freeze.
        self._cap_pegged_since: float | None = None
        self._last_cap_warning_ts: float = 0.0

    @property
    def is_paper(self) -> bool:
        return self._is_paper

    # ─────────────────────────────────────────────────────────────────
    #  Public accessors used by the dashboard
    # ─────────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    @property
    def config(self) -> Config:
        return self._config

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    @property
    def scanner(self) -> MarketScanner:
        return self._scanner

    @property
    def strategies(self) -> list[BaseStrategy]:
        return self._strategies

    @property
    def position_manager(self) -> PositionManager:
        return self._positions

    @property
    def data_pipeline(self) -> DataPipeline:
        return self._pipeline

    @property
    def exchange_feed(self) -> ExchangeFeed:
        return self._exchange_feed

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    @property
    def execution(self) -> ExecutionEngine:
        return self._execution

    @property
    def maker(self) -> MakerOrchestrator | None:
        """Maker-mode orchestrator (None when config.maker.enabled=False)."""
        return self._maker

    # ─────────────────────────────────────────────────────────────────
    #  Lifecycle
    # ─────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start every component and the trading loop."""
        logger.info(
            "bot_starting",
            mode=self._config.bot.mode,
            bankroll_usd=self._config.risk.bankroll_usd,
            strategies=[s.name for s in self._strategies],
        )

        await self._client.start()
        await self._ws.start()
        await self._exchange_feed.start()
        await self._scanner.start()
        await self._pipeline.start()
        await self._alerts.start()
        await self._storage.initialize()
        # Paper-mode trade history is ephemeral — clear it so the dashboard
        # doesn't carry blow-ups (or any prior trades) from earlier sessions
        # into a fresh $0 P&L. Live mode persists.
        if self._is_paper:
            await self._storage.clear_session_data()

        # Event subscriptions
        self._event_bus.subscribe("order_filled", self._on_order_filled)
        self._event_bus.subscribe("market_discovered", self._on_market_discovered)
        self._event_bus.subscribe("market_removed", self._on_market_removed)

        self._running = True
        self._tasks.append(asyncio.create_task(self._trading_loop()))
        self._tasks.append(asyncio.create_task(self._exits_loop()))
        self._tasks.append(asyncio.create_task(self._settlement_backfill_loop()))
        self._tasks.append(asyncio.create_task(self._auto_close_loop()))
        self._tasks.append(asyncio.create_task(self._daily_summary_loop()))
        self._tasks.append(asyncio.create_task(self._status_log_loop()))

        if self._maker is not None:
            await self._maker.start()
        if self._telegram is not None:
            await self._telegram.start()

        if self._config.features_retention.enabled:
            self._tasks.append(
                asyncio.create_task(self._features_cleanup_loop())
            )

        logger.info("bot_started", paper=self._is_paper)

    async def stop(self) -> None:
        """Graceful shutdown with drain window."""
        logger.info("bot_stopping")
        self._running = False

        # Cancel main tasks first; let them drain via the running flag
        try:
            await asyncio.wait_for(self._drain_in_flight(), timeout=_SHUTDOWN_DRAIN_S)
        except TimeoutError:
            logger.warning("shutdown_drain_timeout")

        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        try:
            await self._execution.cancel_all()
        except Exception as e:
            logger.warning("cancel_all_err", error=str(e))

        await self._stop_maker_subsystem()

        try:
            await self._scanner.stop()
            await self._pipeline.stop()
            await self._exchange_feed.stop()
            await self._ws.stop()
            await self._alerts.stop()
            await self._client.close()
        except Exception as e:
            logger.warning("shutdown_component_err", error=str(e))

        try:
            await self._storage.close()
        except Exception:
            pass
        try:
            self._features.close()
        except Exception:
            pass

        shutdown_decision_log()
        logger.info("bot_stopped")

    async def _drain_in_flight(self) -> None:
        """Best-effort: yield briefly so any in-flight tool calls can ack."""
        await asyncio.sleep(0.5)

    async def _stop_maker_subsystem(self) -> None:
        """Stop maker orchestrator and telegram alerter, swallowing errors."""
        if self._maker is not None:
            try:
                await self._maker.stop()
            except Exception as e:
                logger.warning("maker_stop_err", error=str(e))
        if self._telegram is not None:
            try:
                await self._telegram.stop()
            except Exception as e:
                logger.warning("telegram_stop_err", error=str(e))

    # ─────────────────────────────────────────────────────────────────
    #  Strategy construction
    # ─────────────────────────────────────────────────────────────────

    def _build_strategies(self) -> list[BaseStrategy]:
        out: list[BaseStrategy] = []
        for item in self._config.strategies.enabled:
            cls = STRATEGY_REGISTRY.get(item.name)
            if cls is None:
                logger.warning("unknown_strategy", name=item.name)
                continue
            try:
                instance = cls(**(item.params or {}))
                out.append(instance)
                logger.info("strategy_loaded", name=item.name, weight=item.weight)
            except Exception as e:
                logger.error(
                    "strategy_init_err",
                    name=item.name,
                    error=str(e),
                    error_type=type(e).__name__,
                )
        return out

    # ─────────────────────────────────────────────────────────────────
    #  Event handlers
    # ─────────────────────────────────────────────────────────────────

    async def _on_market_discovered(self, market: Market, **_) -> None:
        # Register first so any orderbook frames that arrive between the
        # ws subscribe and the first cycle land in a buffer.
        self._pipeline.register_market(market)
        for token_id in market.token_ids:
            await self._ws.subscribe_market(token_id)

    async def _on_market_removed(self, market_id: str, **_) -> None:
        # Pull token_ids from the pipeline market cache (works even with no
        # orderbook yet), unsubscribe ws, then unregister from the pipeline.
        market = self._pipeline.get_market(market_id)
        if market and market.token_ids:
            for token_id in market.token_ids:
                await self._ws.unsubscribe_market(token_id)
        self._pipeline.unregister_market(market_id)

    async def _on_order_filled(self, order: Order, **_) -> None:
        """Update positions, sizing-state, and storage when a fill arrives."""
        try:
            self._positions.update_from_fill(order)
            await self._storage.save_order(order)

            # If a maker_edge fill, update its inventory tracker
            if order.strategy.startswith("maker_edge"):
                strat = self._get_strategy_by_name("maker_edge")
                if strat is not None and isinstance(strat, MakerEdgeStrategy):
                    delta = order.filled_size if order.side == Side.BUY else -order.filled_size
                    strat.update_inventory(
                        market_id=order.market_id,
                        delta_shares=delta,
                        fill_price=order.avg_fill_price,
                    )

            # Reset maker_edge inventory whenever no position remains on the
            # market — covers both explicit exit fills AND non-exit fills
            # that fully netted out an opposite-side position (e.g. a SELL
            # quote that closed the entire BUY position).
            still_open = any(
                p.market_id == order.market_id
                for p in self._positions.portfolio.positions
            )
            if not still_open:
                strat = self._get_strategy_by_name("maker_edge")
                if strat is not None and isinstance(strat, MakerEdgeStrategy):
                    strat.reset_inventory(order.market_id)

        except Exception as e:
            logger.error("on_order_filled_err", error=str(e))

    def _get_strategy_by_name(self, name: str) -> BaseStrategy | None:
        for s in self._strategies:
            if s.name == name:
                return s
        return None

    # ─────────────────────────────────────────────────────────────────
    #  Trading loop
    # ─────────────────────────────────────────────────────────────────

    async def _trading_loop(self) -> None:
        loop_interval_s = self._config.execution.loop_interval_ms / 1000.0
        while self._running:
            t_start = time.monotonic()
            self._cycle_count += 1

            try:
                self._prune_force_closed()
                await self._run_one_cycle()
            except Exception as e:
                logger.error(
                    "trading_loop_err",
                    error=str(e),
                    error_type=type(e).__name__,
                )

            elapsed = time.monotonic() - t_start
            sleep_s = max(0.0, loop_interval_s - elapsed)
            await asyncio.sleep(sleep_s)

    async def _run_one_cycle(self) -> None:
        if not self._circuit_breaker.is_trading_allowed:
            return

        self._check_cap_pegged()

        markets = list(self._scanner.active_markets.values())
        if not markets:
            return

        max_per_cycle = self._config.execution.max_trades_per_cycle
        max_per_market = self._config.execution.max_trades_per_market_per_cycle
        trades_this_cycle = 0
        trades_per_market: dict[str, int] = {}

        for market in markets:
            if trades_this_cycle >= max_per_cycle:
                break
            if market.id in self._force_closed_markets:
                continue

            snapshot = self._pipeline.get_snapshot(market.id)
            if snapshot is None:
                continue

            # Mark-to-market all open positions in this market
            self._positions.update_prices(market.id, snapshot.orderbook.mid_price)

            # Strategy fan-out + aggregation
            signals = await self._collect_signals(snapshot)
            if not signals:
                continue

            # Aggregator returns a list keyed per market; per-market loop
            # means at most one survives for this market_id.
            agg_list = self._aggregator.aggregate(signals)
            agg = agg_list[0] if agg_list else None
            if agg is None:
                continue

            # Cooldown
            now = time.time()
            if self._config.execution.cooldown_per_market:
                last = self._market_cooldown.get(market.id, 0.0)
                if now - last < self._config.risk.min_trade_interval_seconds:
                    last_sig = self._last_signal_ts.get(market.id, 0.0)
                    age = now - last_sig
                    if not (
                        agg.confidence >= self._config.execution.high_conf_override_threshold
                        and age >= self._config.execution.high_conf_override_after_s
                    ):
                        emit_decision(
                            cycle_id=f"{self._cycle_count:05d}",
                            strategy=f"agg({agg.strategy})",
                            market_id=market.id,
                            decision="BLOCKED",
                            reason=BlockReason.COOLDOWN,
                            confidence=agg.confidence,
                        )
                        continue

            self._last_signal_ts[market.id] = now

            if trades_per_market.get(market.id, 0) >= max_per_market:
                continue

            # Risk gate (entry)
            timeframe = self._scanner.get_timeframe(market.id) or ""

            # Compute Kelly size first so we can pre-check projected exposure
            sizing = self._risk.kelly_size_for_signal(
                agg,
                bankroll=self._effective_bankroll(),
                timeframe=timeframe,
            )
            sized_usd = sizing.size_usd * self._circuit_breaker.size_multiplier
            if sized_usd <= 0:
                continue

            # Dual-direction arb opens TWO positions per signal (YES+NO legs);
            # the position cap must reserve both slots, otherwise a single
            # dual signal can push the portfolio past max_positions and
            # permanently lock out subsequent entries (those legs are
            # held-to-expiry).
            agg_meta = agg.metadata or {}
            projected_positions = 2 if agg_meta.get("is_dual_direction") else 1

            ok, reason = self._risk.can_open_position(
                self._positions.portfolio,
                agg,
                timeframe=timeframe,
                projected_notional=sized_usd,
                projected_positions=projected_positions,
            )
            if not ok:
                continue

            # Build order with metadata propagated from signal
            order = self._build_order(agg, sized_usd, snapshot)
            if order is None:
                continue

            executed = await self._execution.execute_order(
                order, self._positions.portfolio
            )

            if executed.status.value == "FILLED":
                trades_this_cycle += 1
                trades_per_market[market.id] = trades_per_market.get(market.id, 0) + 1
                self._market_cooldown[market.id] = now
                # Track exposure delta
                self._risk.record_open_exposure(
                    agg.strategy, executed.filled_size * executed.avg_fill_price
                )
                # Log features (best-effort)
                try:
                    self._features.log_signal(snapshot, agg, executed)
                except Exception:
                    pass

    async def _collect_signals(self, snapshot: MarketSnapshot) -> list[Signal]:
        """Run every strategy concurrently and gather their non-None signals."""
        coros = [s.evaluate(snapshot) for s in self._strategies]
        results = await asyncio.gather(*coros, return_exceptions=True)
        out: list[Signal] = []
        for s, r in zip(self._strategies, results):
            if isinstance(r, Exception):
                logger.warning("strategy_err", name=s.name, error=str(r))
                continue
            if r is not None:
                out.append(r)
        return out

    def _build_order(
        self,
        signal: Signal,
        sized_usd: float,
        snapshot: MarketSnapshot,
    ) -> Order | None:
        """Construct an Order from a signal, propagating signal.metadata."""
        # Find the right token_id for the chosen outcome
        outcome_idx = 0
        try:
            outcomes_lower = [o.lower() for o in snapshot.market.outcomes]
            outcome_idx = outcomes_lower.index(signal.outcome.lower())
        except (ValueError, AttributeError):
            outcome_idx = 0

        if outcome_idx >= len(snapshot.market.token_ids):
            logger.warning(
                "build_order_token_missing",
                m=snapshot.market.id[:12],
                outcome=signal.outcome,
                tokens=len(snapshot.market.token_ids),
            )
            return None

        token_id = snapshot.market.token_ids[outcome_idx]

        # Inject NO-leg token_id into metadata for dual-direction arb
        merged_meta = dict(signal.metadata or {})
        if merged_meta.get("is_dual_direction") and not merged_meta.get("no_token_id"):
            other_idx = 1 - outcome_idx
            if 0 <= other_idx < len(snapshot.market.token_ids):
                merged_meta["no_token_id"] = snapshot.market.token_ids[other_idx]

        # Convert sized_usd → shares at signal.target_price
        if signal.target_price <= 0:
            return None
        size_shares = sized_usd / signal.target_price

        side = Side.BUY if signal.direction == Direction.BUY else Side.SELL

        return Order(
            market_id=signal.market_id,
            token_id=token_id,
            side=side,
            price=signal.target_price,
            size=size_shares,
            order_type=OrderType.GTC,
            strategy=signal.strategy,
            signal_id=f"sig-{int(time.time()*1000)}",
            metadata=merged_meta,
        )

    def _effective_bankroll(self) -> float:
        """Paper bankroll = config; live bankroll = wallet balance."""
        if self._is_paper:
            return float(self._config.risk.bankroll_usd)
        # Live mode would query wallet — for now, fall back to config too
        return float(self._config.risk.bankroll_usd)

    def _prune_force_closed(self) -> None:
        if not self._force_closed_markets:
            return
        now = time.time()
        expired = [m for m, ts in self._force_closed_markets.items() if ts < now]
        for m in expired:
            self._force_closed_markets.pop(m, None)

    def _check_cap_pegged(self) -> None:
        """Log a warning when the position cap has been full for too long.

        Without this signal the bot looks identical (RUNNING, dashboard
        ticking) whether it's actively scanning or sitting locked out by
        held-to-expiry positions.
        """
        cap = self._config.risk.max_positions
        n_open = len(self._positions.portfolio.positions)
        now = time.time()

        if n_open >= cap:
            if self._cap_pegged_since is None:
                self._cap_pegged_since = now
            elapsed = now - self._cap_pegged_since
            # Re-warn every 60s while the cap stays pegged so the operator
            # sees ongoing throttling, not just the first occurrence.
            if elapsed >= 60.0 and (now - self._last_cap_warning_ts) >= 60.0:
                self._last_cap_warning_ts = now
                logger.warning(
                    "position_cap_pegged",
                    open=n_open,
                    cap=cap,
                    pegged_for_s=round(elapsed, 0),
                    note="no new entries will be accepted until a slot frees",
                )
        else:
            if self._cap_pegged_since is not None:
                logger.info(
                    "position_cap_released",
                    open=n_open,
                    cap=cap,
                    was_pegged_for_s=round(now - self._cap_pegged_since, 0),
                )
            self._cap_pegged_since = None

    # ─────────────────────────────────────────────────────────────────
    #  Exits + auto-close + settlement
    # ─────────────────────────────────────────────────────────────────

    async def _exits_loop(self) -> None:
        """Periodically check open positions for exit conditions."""
        while self._running:
            try:
                exits = self._positions.check_exits()
                for ex in exits:
                    await self._execute_exit(ex.position, ex.reason, force=False)
            except Exception as e:
                logger.warning("exits_loop_err", error=str(e))
            await asyncio.sleep(1.0)

    async def _auto_close_loop(self) -> None:
        """Force-close positions in markets that are about to expire."""
        threshold_s = self._config.execution.auto_close_before_expiry_s
        while self._running:
            try:
                now = datetime.utcnow()
                for p in list(self._positions.portfolio.positions):
                    market = self._scanner.active_markets.get(p.market_id)
                    if market is None:
                        continue
                    end = market.end_date
                    if getattr(end, "tzinfo", None) is not None:
                        end = end.replace(tzinfo=None)
                    t_rem = (end - now).total_seconds()
                    if t_rem <= threshold_s and p.market_id not in self._force_closed_markets:
                        await self._execute_exit(
                            p, f"auto_close_before_expiry (t_rem={t_rem:.0f}s)",
                            force=True,
                        )
                        # Mark as force-closed, expire from set after TTL
                        self._force_closed_markets[p.market_id] = (
                            time.time() + _FORCE_CLOSED_TTL_S
                        )
            except Exception as e:
                logger.warning("auto_close_loop_err", error=str(e))
            await asyncio.sleep(2.0)

    async def _execute_exit(self, position, reason: str, force: bool = False) -> None:
        """Submit an exit order for an open position."""
        snapshot = self._pipeline.get_snapshot(position.market_id)
        if snapshot is None and not force:
            return

        # Exit price: hit the appropriate side of the position's OWN token
        # book, never the opposite leg's. Binary markets have two tokens with
        # mirror books (≈1.0 apart); reading the wrong one would close the
        # position at ~the inverse price, which is catastrophic.
        position_ob = self._pipeline.get_orderbook(
            position.market_id, position.token_id,
        )
        if position_ob is not None:
            if position.side == Side.BUY:
                exit_price = position_ob.best_bid or position.current_price
            else:
                exit_price = position_ob.best_ask or position.current_price
        elif snapshot is not None and (
            # Fallback: only use the snapshot's primary book if it actually
            # belongs to this position's token (single-token markets / tests).
            getattr(snapshot.orderbook, "market_id", "") == position.token_id
        ):
            if position.side == Side.BUY:
                exit_price = snapshot.orderbook.best_bid or position.current_price
            else:
                exit_price = snapshot.orderbook.best_ask or position.current_price
        else:
            exit_price = position.current_price or position.avg_entry_price

        exit_price = max(0.001, min(0.999, exit_price))

        # Sanity bound: refuse to exit at a price more than 30% adverse to
        # entry unless this is a forced auto-close. Catches stale-book and
        # cross-token mispricing before they realise a 90%+ loss.
        if not force:
            entry = position.avg_entry_price
            if entry > 0:
                if position.side == Side.BUY:
                    adverse = (entry - exit_price) / entry
                else:
                    adverse = (exit_price - entry) / entry
                if adverse > 0.30:
                    logger.warning(
                        "exit_price_sanity_block",
                        m=position.market_id[:12],
                        strat=position.strategy,
                        entry=round(entry, 4),
                        exit_px=round(exit_price, 4),
                        adverse_pct=round(adverse * 100, 1),
                        reason=reason,
                        note="refusing to realise; will retry on next exits cycle with fresh book",
                    )
                    return

        # Flip side
        side = Side.SELL if position.side == Side.BUY else Side.BUY

        strat_label = "auto_exit" if force else "exit"
        order = Order(
            market_id=position.market_id,
            token_id=position.token_id,
            side=side,
            price=exit_price,
            size=position.size,
            order_type=OrderType.GTC,
            strategy=f"{strat_label}_{position.strategy}",
            signal_id=f"exit-{int(time.time()*1000)}",
            metadata={"exit_reason": reason, "force": force},
        )

        # Snapshot realised P&L *before* the exit fill so we can compute the
        # exact delta this exit produced. Without this the RiskManager and
        # CircuitBreaker never see real P&L and their loss-tracking gates
        # are silently disabled.
        realized_before = self._positions.portfolio.realized_pnl

        try:
            executed = await self._execution.execute_order(
                order, self._positions.portfolio
            )
            if executed.status.value == "FILLED":
                pnl_delta = self._positions.portfolio.realized_pnl - realized_before
                self._risk.record_pnl(position.strategy, pnl_delta)
                self._circuit_breaker.record_trade_result(pnl_delta)
                # Daily-loss check
                if (
                    self._positions.portfolio.daily_pnl
                    <= -abs(self._config.risk.max_daily_loss)
                ):
                    self._circuit_breaker.record_daily_loss_breach()
            elif executed.status.value == "REJECTED":
                logger.error(
                    "exit_rejected",
                    m=position.market_id[:12],
                    reason=reason,
                )
        except LiveTradingNotImplementedError:
            logger.error(
                "exit_live_not_implemented",
                m=position.market_id[:12],
            )
        except Exception as e:
            logger.error(
                "execute_exit_err",
                m=position.market_id[:12],
                reason=reason,
                error=str(e),
            )

    async def _settlement_backfill_loop(self) -> None:
        """Reconcile positions in resolved markets against actual settlement."""
        while self._running:
            try:
                # Look for positions whose markets disappeared from scanner
                active_ids = set(self._scanner.active_markets)
                for p in list(self._positions.portfolio.positions):
                    if p.market_id not in active_ids:
                        # Market gone; if not already force-closed, do it now
                        if p.market_id not in self._force_closed_markets:
                            await self._execute_exit(
                                p, "settlement_backfill", force=True
                            )
                            self._force_closed_markets[p.market_id] = (
                                time.time() + _FORCE_CLOSED_TTL_S
                            )
            except Exception as e:
                logger.warning("settlement_backfill_err", error=str(e))
            await asyncio.sleep(15.0)

    # ─────────────────────────────────────────────────────────────────
    #  Periodic maintenance
    # ─────────────────────────────────────────────────────────────────

    async def _features_cleanup_loop(self) -> None:
        cfg = self._config.features_retention
        interval_s = max(1, cfg.cleanup_interval_hours) * 3600
        await asyncio.sleep(60)  # initial settle
        while self._running:
            try:
                deleted = self._features.cleanup_old_rows(keep_days=cfg.keep_days)
                logger.info("features_cleanup", deleted_rows=deleted, keep_days=cfg.keep_days)
            except Exception as e:
                logger.warning("features_cleanup_err", error=str(e))
            await asyncio.sleep(interval_s)

    async def _status_log_loop(self) -> None:
        """Emit a one-line bot-status heartbeat every minute.

        The bot used to look identical (RUNNING + ticking timestamps) whether
        it was actively trading, locked out by the position cap, or sitting
        with a dead BTC feed. This heartbeat surfaces what the bot is
        actually doing — feed health, open positions, recent block reasons —
        so the operator can tell *why* no trades are firing without having
        to grep the decision log.
        """
        await asyncio.sleep(30)  # initial settle
        while self._running:
            try:
                self._emit_status()
            except Exception as e:
                logger.debug("status_log_err", error=str(e))
            await asyncio.sleep(60)

    def _emit_status(self) -> None:
        portfolio = self._positions.portfolio
        cap = self._config.risk.max_positions
        n_open = len(portfolio.positions)
        feed = self._exchange_feed
        feed_age = feed.feed_age_s
        cap_pegged_for = None
        if self._cap_pegged_since is not None:
            cap_pegged_for = round(time.time() - self._cap_pegged_since, 0)
        logger.info(
            "bot_status",
            mode=self._config.bot.mode,
            trading_allowed=self._circuit_breaker.is_trading_allowed,
            active_markets=len(self._scanner.active_markets),
            open_positions=n_open,
            max_positions=cap,
            cap_pegged_for_s=cap_pegged_for,
            realized_pnl=round(portfolio.realized_pnl, 2),
            daily_pnl=round(portfolio.daily_pnl, 2),
            btc_price=round(feed.last_price, 2) if feed.last_price > 0 else None,
            btc_feed_age_s=round(feed_age, 0) if feed_age is not None else None,
            btc_feed_stale=feed.is_stale,
            btc_feed_source=feed.last_source or None,
            top_block_reasons=block_summary(top_n=5),
        )

    async def _daily_summary_loop(self) -> None:
        """Log a daily summary of decision-log block reasons + portfolio state."""
        target_hour = self._config.monitoring.daily_summary_hour
        while self._running:
            now = datetime.now(UTC)
            target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
            if now >= target:
                target = target + timedelta(days=1)
            sleep_s = (target - now).total_seconds()
            try:
                await asyncio.sleep(sleep_s)
                self._emit_daily_summary()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("daily_summary_err", error=str(e))
                await asyncio.sleep(60)

    def _emit_daily_summary(self) -> None:
        portfolio = self._positions.portfolio
        summary = {
            "open_positions": len(portfolio.positions),
            "daily_pnl": round(portfolio.daily_pnl, 2),
            "realized_pnl": round(portfolio.realized_pnl, 2),
            "total_exposure": round(portfolio.total_exposure, 2),
            "block_reasons": block_summary(top_n=10),
        }
        logger.info("daily_summary", **summary)
        try:
            asyncio.create_task(
                self._alerts.send_alert(
                    level=__import__("polybot.data.models", fromlist=["AlertLevel"]).AlertLevel.INFO,
                    message="Daily summary",
                    data=summary,
                )
            )
        except Exception:
            pass


# ───────────────────────────────────────────────────────────────────────────
#  Entrypoint
# ───────────────────────────────────────────────────────────────────────────


async def _run(config_path: str) -> None:
    config = load_config(config_path)
    bot = Bot(config)
    stop_event = asyncio.Event()

    def _on_signal(*_args) -> None:
        logger.info("signal_received_stopping")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            # Windows doesn't support signal handlers via asyncio.
            signal.signal(sig, lambda *_a: stop_event.set())

    await bot.start()
    try:
        await stop_event.wait()
    finally:
        await bot.stop()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="BTC-Bot trading bot")
    parser.add_argument("--config", default="config.yaml", help="Config YAML path")
    args = parser.parse_args()

    # Health monitor warm-up
    get_monitor()

    # Apply BOT_LOG_LEVEL env if set
    log_level = os.environ.get("BOT_LOG_LEVEL", "INFO").upper()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(__import__("logging"), log_level, 20)
        ),
    )

    asyncio.run(_run(args.config))


if __name__ == "__main__":
    main()
