"""Maker-mode orchestrator — wires the V2 components into the bot lifecycle.

When `config.maker.enabled` is True, the Bot owns one of these and starts
its loop alongside the existing trading loop. The orchestrator:

  * picks active 5-min BTC markets out of the scanner,
  * creates a QuoteManager + InventoryManager per market on first sight,
  * recomputes fair value off the live BTC feed each tick,
  * calls QuoteManager.sync_quotes() — paper mode synthesises acks,
    live mode batches into the V2 client,
  * watches feed staleness and triggers cancel-all,
  * surfaces aggregate vitals for the Telegram heartbeat.

Paper-mode fills are simulated by checking whether the orderbook mid has
crossed through any resting quote price since the last tick — accurate
enough for the 4-week validation gate.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from polybot.api.clob_v2_client import ClobV2Client
from polybot.config import MakerConfig
from polybot.data.exchange_feed import ExchangeFeed
from polybot.data.models import Market
from polybot.data.pipeline import DataPipeline
from polybot.diagnostics.decision_log import BlockReason
from polybot.diagnostics.decision_log import emit as emit_decision
from polybot.execution.quote_manager import QuoteManager, QuoteManagerConfig
from polybot.monitoring.latency_tracker import LatencyConfig, LatencyTracker
from polybot.monitoring.paper_validation import PaperValidationGate
from polybot.monitoring.telegram_alerts import HeartbeatVitals, TelegramAlerter
from polybot.positions.inventory import InventoryConfig, InventoryManager
from polybot.scanner.scanner import MarketScanner
from polybot.strategies.fair_value import (
    PriceBuffer1Hz,
    fee_at_price,
    realized_sigma_per_sec,
)
from polybot.strategies.fair_value import fair_value as bs_fair_value
from polybot.strategies.maker_quoting import (
    MakerQuotingConfig,
    MakerQuotingStrategy,
)

logger = structlog.get_logger()


def _as_naive_utc(dt: datetime) -> datetime:
    """Coerce a datetime to naive UTC so it can be subtracted from another.

    Gamma parses market end_dates as tz-aware (the `Z`→`+00:00` swap),
    while the pipeline stamps orderbooks with naive `datetime.utcnow()`.
    Subtracting the two raises "can't subtract offset-naive and
    offset-aware datetimes". Normalising both operands here is the fix.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


@dataclass
class _RoundTrip:
    gross_pnl_usd: float
    fees_paid_usd: float


class _RoundTripTracker:
    """YES-equivalent position tracker for paper-mode maker fills.

    Every fill is converted to a signed YES position at a YES-equivalent
    price, since a binary market's two tokens are mirror images:

        YES BUY  @ p  ->  long  YES  @ p
        YES SELL @ p  ->  short YES  @ p
        NO  BUY  @ p  ->  short YES  @ (1 - p)   (buying NO == shorting YES)
        NO  SELL @ p  ->  long  YES  @ (1 - p)

    We hold ONE net position with a volume-weighted average entry price.
    A fill that reduces the magnitude realizes P&L for the closed portion
    at the fill price; a fill that grows or flips the position updates the
    average entry.

    Crucially, inventory still open when the market rolls off is realized
    via `realize_remaining(exit_price_yes=...)`. That is where one-sided
    "caught a falling knife" accumulation (YES bids filled all the way
    down a crashing mid) finally books its real loss — the previous
    lot-pairing model only ever realized the hedged-pair legs and let the
    naked inventory's loss vanish, which is what produced the fake
    +13%-in-15-minutes paper P&L.
    """

    def __init__(self, fee_theta: float = 0.072) -> None:
        self._fee_theta = fee_theta
        self._net: float = 0.0   # signed YES-equivalent shares (+long / -short)
        self._avg: float = 0.0   # volume-weighted avg YES entry price

    @property
    def fee_theta(self) -> float:
        return self._fee_theta

    @property
    def open_lot_count(self) -> int:
        # Back-compat with existing tests: 0 when flat, 1 when a position is open.
        return 0 if abs(self._net) < 1e-12 else 1

    @property
    def net_signed_inventory(self) -> float:
        return self._net

    @property
    def avg_entry_yes(self) -> float:
        return self._avg

    @staticmethod
    def _to_yes(side_yes: bool, is_buy: bool, price: float) -> tuple[float, float]:
        """Map a fill to (direction, yes_equivalent_price).

        direction is +1 for a long-YES contribution, -1 for short-YES.
        """
        if side_yes:
            return (1.0 if is_buy else -1.0), price
        # NO token: buying NO shorts YES; price mirrors to (1 - p).
        return (-1.0 if is_buy else 1.0), (1.0 - price)

    def _fees(self, price_a: float, price_b: float, shares: float) -> float:
        return (
            fee_at_price(price_a, theta=self._fee_theta)
            + fee_at_price(price_b, theta=self._fee_theta)
        ) * shares

    def on_fill(
        self,
        *,
        side_yes: bool,
        is_buy: bool,
        shares: float,
        price: float,
    ) -> list[_RoundTrip]:
        """Record a fill, return any round-trips realized by it (may be empty)."""
        if shares <= 0:
            return []
        direction, yes_price = self._to_yes(side_yes, is_buy, price)
        signed = direction * shares
        round_trips: list[_RoundTrip] = []

        if abs(self._net) < 1e-12 or (self._net > 0) == (signed > 0):
            # Flat, or same direction → grow the position and re-average.
            total = abs(self._net) + abs(signed)
            self._avg = (abs(self._net) * self._avg + abs(signed) * yes_price) / total
            self._net += signed
            return round_trips

        # Opposite direction → realize P&L against the existing position for
        # the overlapping portion, at the fill's YES-equivalent price.
        closing = min(abs(self._net), abs(signed))
        if self._net > 0:
            gross = (yes_price - self._avg) * closing      # closed a long
        else:
            gross = (self._avg - yes_price) * closing      # closed a short
        round_trips.append(
            _RoundTrip(
                gross_pnl_usd=gross,
                fees_paid_usd=self._fees(self._avg, yes_price, closing),
            )
        )

        residual = abs(signed) - closing
        self._net += signed
        if abs(self._net) < 1e-12:
            self._net = 0.0
            self._avg = 0.0
        elif residual > 1e-12:
            # The fill flipped the position; the residual opens fresh.
            self._avg = yes_price
        return round_trips

    def realize_remaining(self, *, exit_price_yes: float) -> _RoundTrip | None:
        """Close any open inventory at a YES exit price (flatten / expiry).

        Returns the realized round-trip, or None if already flat. This is
        the call that books the naked-inventory loss the old model dropped.
        """
        if abs(self._net) < 1e-12:
            return None
        exit_px = max(0.0, min(1.0, exit_price_yes))
        closing = abs(self._net)
        if self._net > 0:
            gross = (exit_px - self._avg) * closing
        else:
            gross = (self._avg - exit_px) * closing
        rt = _RoundTrip(
            gross_pnl_usd=gross,
            fees_paid_usd=self._fees(self._avg, exit_px, closing),
        )
        self._net = 0.0
        self._avg = 0.0
        return rt


@dataclass
class _MarketState:
    market: Market
    quote_manager: QuoteManager
    inventory: InventoryManager
    strike: float = 0.0
    last_mid: float = 0.0
    fills_today: int = 0
    round_trip_tracker: _RoundTripTracker = field(
        default_factory=_RoundTripTracker
    )


@dataclass
class MakerVitals:
    quote_uptime_pct: float = 0.0
    fills_today: int = 0
    net_inventory_shares: float = 0.0
    p95_latency_ms: float = 0.0
    active_markets: int = 0
    last_sync_action: str = "init"
    extras: dict[str, object] = field(default_factory=dict)


class MakerOrchestrator:
    """Lifecycle owner for the V2 maker-quoting subsystem."""

    def __init__(
        self,
        *,
        maker_cfg: MakerConfig,
        scanner: MarketScanner,
        pipeline: DataPipeline,
        exchange_feed: ExchangeFeed,
        is_paper: bool = True,
        client: ClobV2Client | None = None,
        telegram: TelegramAlerter | None = None,
        validation_gate: PaperValidationGate | None = None,
    ) -> None:
        self._cfg = maker_cfg
        self._scanner = scanner
        self._pipeline = pipeline
        self._exchange_feed = exchange_feed
        self._is_paper = is_paper
        self._client = client
        self._telegram = telegram
        self._validation = validation_gate

        self._strategy = MakerQuotingStrategy(
            MakerQuotingConfig(
                min_half_spread_cents=maker_cfg.min_half_spread_cents,
                adverse_selection_buffer_cents=maker_cfg.adverse_selection_buffer_cents,
            )
        )
        self._latency = LatencyTracker(
            LatencyConfig(
                warn_p95_ms=maker_cfg.latency_warn_p95_ms,
                kill_p95_ms=maker_cfg.latency_kill_p95_ms,
                sustained_breach_seconds=maker_cfg.latency_sustained_breach_s,
            )
        )
        self._states: dict[str, _MarketState] = {}
        self._price_buffer = PriceBuffer1Hz(max_seconds=1800)
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._vitals = MakerVitals()
        self._loop_uptime_total = 0.0
        self._loop_synced_total = 0.0
        self._loop_started_at = 0.0

    # ─────────────────────────────────────────────────────────────────
    #  Lifecycle
    # ─────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop_started_at = time.monotonic()
        if self._telegram is not None:
            self._telegram.set_vitals_provider(self._collect_vitals_for_heartbeat)
        self._tasks.append(asyncio.create_task(self._main_loop()))
        self._tasks.append(asyncio.create_task(self._feed_watchdog_loop()))
        if self._validation is not None:
            # Daily-returns ticker + periodic auto-save are gate-only — no
            # point starting them without somewhere to write the result.
            self._tasks.append(asyncio.create_task(self._daily_return_loop()))
            self._tasks.append(asyncio.create_task(self._auto_save_loop()))
        logger.info("maker_orchestrator_started", is_paper=self._is_paper)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for state in list(self._states.values()):
            # Book any open inventory before tearing the state down so the
            # gate's P&L reflects positions that were live at shutdown.
            self._realize_position_on_close(state, reason="shutdown")
            try:
                await state.quote_manager.cancel_all()
            except Exception:
                pass
        logger.info("maker_orchestrator_stopped")

    def _realize_position_on_close(
        self, state: _MarketState, *, reason: str
    ) -> None:
        """Realize a market's open inventory at its last mid and record the
        round-trip on the validation gate. Idempotent once flat."""
        exit_px = state.last_mid if state.last_mid > 0 else 0.5
        rt = state.round_trip_tracker.realize_remaining(exit_price_yes=exit_px)
        if rt is None:
            return
        try:
            state.inventory.reset()
        except Exception:
            pass
        logger.info(
            "maker_position_realized",
            market=state.market.id[:12],
            reason=reason,
            exit_mid=round(exit_px, 4),
            gross_pnl=round(rt.gross_pnl_usd, 4),
            fees=round(rt.fees_paid_usd, 4),
        )
        if self._validation is not None:
            self._validation.record_round_trip(
                gross_pnl_usd=rt.gross_pnl_usd,
                fees_paid_usd=rt.fees_paid_usd,
                dynamic_fee_fetched=True,
            )

    # ─────────────────────────────────────────────────────────────────
    #  Loops
    # ─────────────────────────────────────────────────────────────────

    async def _main_loop(self) -> None:
        """Recompute fair value + sync quotes for each active maker market."""
        while self._running:
            try:
                self._ingest_btc_tick()
                await self._maintain_states()
                await self._sync_all()
                await self._check_latency_kill_switch()
                self._update_validation_metrics()
            except Exception as e:
                logger.error(
                    "maker_loop_err",
                    err=str(e)[:120],
                    err_type=type(e).__name__,
                )
                if self._validation is not None:
                    self._validation.record_unhandled_exception()
                if self._telegram is not None:
                    await self._telegram.unhandled_exception(
                        where="maker_loop", err=str(e)
                    )
            # 200ms cadence: ~5 syncs/sec per market; tuned to stay well under
            # the V2 post_order per-10s burst limit even with 5 markets active.
            await asyncio.sleep(0.2)

    async def _check_latency_kill_switch(self) -> None:
        """If p95 has sustained a breach, cancel everything and alert."""
        if not self._latency.should_disable_live():
            return
        p95 = self._latency.p95
        logger.warning("maker_latency_kill_switch", p95_ms=round(p95, 1))
        for state in list(self._states.values()):
            try:
                await state.quote_manager.cancel_all()
            except Exception:
                pass
        if self._telegram is not None:
            await self._telegram.latency_breach(p95_ms=p95)
        # Reset the tracker so we don't fire repeatedly each tick — the
        # cancel-all has already reset the failure mode.
        self._latency.reset()

    def _update_validation_metrics(self) -> None:
        if self._validation is None:
            return
        v = self.vitals()
        self._validation.record_quote_uptime(v.quote_uptime_pct)
        self._validation.record_p95_latency(v.p95_latency_ms)

    async def _daily_return_loop(self) -> None:
        """Once-per-UTC-day, record (today's net P&L) / bankroll as a return.

        Without this loop, `daily_returns_pct` stays empty and the gate's
        Sharpe metric is permanently zero — which silently blocks the
        validation status from ever turning READY.
        """
        if self._validation is None:
            return
        # Snapshot the gate's net P&L at the boundary; the next boundary's
        # delta is the day's return as a fraction of the bankroll baseline.
        last_recorded_pnl = self._validation.state.net_pnl_usd
        last_recorded_day = self._utc_day_index(time.time())
        bankroll = max(1.0, self._validation_bankroll())
        while self._running:
            try:
                await asyncio.sleep(self._seconds_until_next_utc_midnight())
                if not self._running or self._validation is None:
                    break
                cur_pnl = self._validation.state.net_pnl_usd
                cur_day = self._utc_day_index(time.time())
                if cur_day != last_recorded_day:
                    day_return_pct = (cur_pnl - last_recorded_pnl) / bankroll
                    self._validation.record_daily_return(day_return_pct)
                    self._validation.save()
                    last_recorded_pnl = cur_pnl
                    last_recorded_day = cur_day
                    logger.info(
                        "validation_daily_return_recorded",
                        day=cur_day,
                        ret_pct=round(day_return_pct * 100, 4),
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("daily_return_loop_err", err=str(e)[:80])
                await asyncio.sleep(60.0)

    async def _auto_save_loop(self) -> None:
        """Persist validation gate state every 5 minutes.

        The gate's 30-day clock is the gating mechanism for the live
        flip; losing it to a crash mid-run would invalidate any progress.
        """
        if self._validation is None:
            return
        while self._running:
            try:
                await asyncio.sleep(300.0)
                if not self._running or self._validation is None:
                    break
                self._validation.save()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("auto_save_loop_err", err=str(e)[:80])

    @staticmethod
    def _utc_day_index(epoch_seconds: float) -> int:
        return int(epoch_seconds // 86400)

    @staticmethod
    def _seconds_until_next_utc_midnight(now: float | None = None) -> float:
        now = now if now is not None else time.time()
        next_midnight = (int(now // 86400) + 1) * 86400
        # Guard against pathological clock skew — at least 1s sleep.
        return max(1.0, next_midnight - now)

    def _validation_bankroll(self) -> float:
        if self._validation is None:
            return 500.0
        return self._validation._starting_equity or 500.0

    async def _feed_watchdog_loop(self) -> None:
        """Cancel all maker quotes when the BTC feed goes silent."""
        threshold = self._cfg.binance_stale_threshold_s
        while self._running:
            await asyncio.sleep(min(1.0, threshold / 2))
            age = self._exchange_feed.feed_age_s
            if age is None or age <= threshold:
                continue
            logger.warning("maker_feed_stale_cancel_all", age_s=round(age, 1))
            for state in list(self._states.values()):
                try:
                    await state.quote_manager.on_feed_disconnect(
                        reason=f"btc_stale_{age:.0f}s"
                    )
                except Exception:
                    pass
            if self._telegram is not None:
                await self._telegram.binance_feed_down(age_s=age)

    # ─────────────────────────────────────────────────────────────────
    #  Per-market state management
    # ─────────────────────────────────────────────────────────────────

    async def _maintain_states(self) -> None:
        """Create QuoteManager/InventoryManager for newly-discovered maker
        markets; tear down state for markets that have rolled off."""
        eligible = self._eligible_markets()
        seen: set[str] = set()
        for market in eligible:
            seen.add(market.id)
            if market.id not in self._states:
                self._states[market.id] = self._build_state(market)
                logger.info(
                    "maker_market_registered",
                    market=market.id[:12],
                    question=market.question[:48],
                )

        # Drop states for markets that have left the eligibility set.
        stale = [mid for mid in self._states if mid not in seen]
        for mid in stale:
            state = self._states.pop(mid)
            # Realize any inventory still open at rolloff against the last
            # observed mid — this is where naked one-sided accumulation
            # (caught-falling-knife) books its real P&L on the gate instead
            # of being silently abandoned with the discarded state.
            self._realize_position_on_close(state, reason="market_rolloff")
            try:
                await state.quote_manager.cancel_all()
            except Exception:
                pass

    def _build_state(self, market: Market) -> _MarketState:
        if len(market.token_ids) < 2:
            # Shouldn't happen for BTC up/down but be defensive
            raise ValueError(
                f"maker requires 2 token_ids; market {market.id} has "
                f"{len(market.token_ids)}"
            )
        qm = QuoteManager(
            market_id=market.id,
            yes_token_id=market.token_ids[0],
            no_token_id=market.token_ids[1],
            strategy=self._strategy,
            client=self._client,
            latency=self._latency,
            config=QuoteManagerConfig(
                stale_threshold_cents=self._cfg.requote_threshold_cents,
                max_quote_lifetime_s=self._cfg.max_quote_lifetime_s,
                flatten_before_expiry_s=self._cfg.flatten_before_expiry_s,
                target_size_shares=self._cfg.target_size_shares,
            ),
            is_paper=self._is_paper,
        )
        inv = InventoryManager(
            market_id=market.id,
            config=InventoryConfig(
                max_inventory_per_side=self._cfg.max_inventory_per_side,
                delta_threshold_uncertain=self._cfg.delta_threshold_uncertain,
                delta_threshold_directional=self._cfg.delta_threshold_directional,
                directional_bet_price=self._cfg.directional_bet_price,
            ),
        )
        return _MarketState(market=market, quote_manager=qm, inventory=inv)

    def _eligible_markets(self) -> list[Market]:
        """Return markets the maker layer will quote on.

        We narrow to the configured prefixes (default ['btc-5m']). For each,
        we pick markets whose slug starts with that prefix.
        """
        prefixes = tuple(self._normalise_prefix(p) for p in self._cfg.primary_markets)
        out: list[Market] = []
        for market in self._scanner.active_markets.values():
            slug = (getattr(market, "slug", "") or "").lower()
            if slug.startswith(prefixes):
                out.append(market)
        return out

    @staticmethod
    def _normalise_prefix(p: str) -> str:
        """Accept user-friendly forms like 'btc-5m' or 'btc-updown-5m'."""
        p = p.strip().lower()
        if p.startswith("btc-updown"):
            return p
        if p == "btc-5m":
            return "btc-updown-5m"
        if p == "btc-15m":
            return "btc-updown-15m"
        if p == "btc-1h":
            return "btc-updown-1h"
        if p == "btc-4h":
            return "btc-updown-4h"
        return p

    # ─────────────────────────────────────────────────────────────────
    #  Tick → fair value → sync
    # ─────────────────────────────────────────────────────────────────

    def _ingest_btc_tick(self) -> None:
        price = self._exchange_feed.last_price
        if price > 0:
            self._price_buffer.add_tick(time.time(), price)

    async def _sync_all(self) -> None:
        if not self._states:
            return
        spot = self._exchange_feed.last_price
        if spot <= 0:
            return
        sigma_per_sec = realized_sigma_per_sec(self._price_buffer.snapshot())
        # Annualise for the strategy's vol_scaling input.
        vol_annual = sigma_per_sec * (365.0 * 24.0 * 3600.0) ** 0.5

        for state in list(self._states.values()):
            await self._sync_one(state, spot, sigma_per_sec, vol_annual)

    async def _sync_one(
        self,
        state: _MarketState,
        spot: float,
        sigma_per_sec: float,
        vol_annual: float,
    ) -> None:
        market = state.market
        snapshot = self._pipeline.get_snapshot(market.id)
        if snapshot is None:
            return
        # Both operands normalised to naive UTC — Gamma end_dates are
        # tz-aware, pipeline timestamps are naive, and subtracting the two
        # raw raises a TypeError on every loop iteration.
        now_utc = _as_naive_utc(snapshot.orderbook.timestamp)
        end_dt = _as_naive_utc(market.end_date)
        t_rem = max(0.0, (end_dt - now_utc).total_seconds())

        # Strike: lock on first sight using the snapshot mid as a proxy.
        # Once locked, derive fair value from spot/strike drift like
        # overshoot_reversion does (the BTC binary fair value).
        if state.strike <= 0 and spot > 0:
            state.strike = spot

        fair = (
            bs_fair_value(spot, state.strike, t_rem, sigma_per_sec)
            if state.strike > 0
            else snapshot.orderbook.mid_price
        )

        # Paper-mode fill simulation: cross check against mid moves.
        if self._is_paper:
            self._paper_fill_sweep(state, snapshot.orderbook.mid_price)
        state.last_mid = snapshot.orderbook.mid_price

        size = self._cfg.target_size_shares
        inv = state.inventory

        # Observability only: note when we're over the soft inventory limit.
        # We no longer hard-return here — that froze BOTH sides and held the
        # adverse position to expiry (the 97%-inventory_limit stall). Instead
        # we pass net/cap into sync_quotes so the strategy suppresses only the
        # side that would grow the position and keeps quoting the reducing
        # side, letting fills flatten us.
        if abs(inv.net_yes_shares) >= self._cfg.max_inventory_per_side:
            emit_decision(
                cycle_id=f"maker-{int(time.time())}",
                strategy="maker_quoting",
                market_id=market.id,
                decision="SKIP",
                reason=BlockReason.INVENTORY_LIMIT,
                mid=snapshot.orderbook.mid_price,
                fair_value=fair,
                extra={"net_inventory": inv.net_yes_shares},
            )

        # Use a fee rate fetched live when in live mode; default to crypto
        # taker theta in paper mode (no network call).
        fee_rate = 0.072
        if not self._is_paper and self._client is not None:
            try:
                fee_rate = await self._client.get_fee_rate(market.token_ids[0])
            except Exception:
                pass

        result = await state.quote_manager.sync_quotes(
            fair_value=fair,
            fee_rate=fee_rate,
            vol_annual=vol_annual,
            time_remaining_s=t_rem,
            inventory_skew_cents=inv.skew_cents() * 100.0,
            size_shares=size,
            net_inventory_shares=inv.net_yes_shares,
            max_inventory_shares=self._cfg.max_inventory_per_side,
        )

        # Flatten window: turn inventory into either a directional bet or
        # a mid-price unwind, depending on |fair - 0.5|.
        if (
            t_rem <= self._cfg.flatten_before_expiry_s
            and inv.net_yes_shares != 0
        ):
            decision = inv.flatten_action(
                fair_value=fair,
                mid=snapshot.orderbook.mid_price,
                time_remaining_s=t_rem,
            )
            emit_decision(
                cycle_id=f"maker-{int(time.time())}",
                strategy="maker_quoting",
                market_id=market.id,
                decision=decision.action.upper(),
                reason=BlockReason.FLATTEN_TRIGGERED,
                mid=snapshot.orderbook.mid_price,
                fair_value=fair,
                extra={
                    "flatten_action": decision.action,
                    "flatten_size": decision.size_shares,
                    "flatten_target": decision.target_price,
                    "flatten_reason": decision.reason,
                },
            )

        # Vitals accounting
        self._loop_uptime_total += 1.0
        if isinstance(result, dict) and result.get("action") == "synced":
            self._loop_synced_total += 1.0

    # ─────────────────────────────────────────────────────────────────
    #  Paper-mode fill simulation
    # ─────────────────────────────────────────────────────────────────

    def _paper_fill_sweep(self, state: _MarketState, mid: float) -> None:
        """Mark a quote 'filled' if the orderbook mid has crossed it.

        For YES-side bids: a fill happens if mid <= quote_price (someone
        sold at our bid).
        For NO-side bids: same logic against (1 - mid).
        """
        qm = state.quote_manager
        filled: list[tuple[str, float, float, bool]] = []  # (oid, shares, price, side_yes)
        for oid, order in list(qm.state.resting.items()):
            side_mid = mid if order.side_yes else (1.0 - mid)
            if side_mid <= order.price:
                filled.append((oid, order.size, order.price, order.side_yes))

        for oid, shares, price, side_yes in filled:
            qm.on_fill(order_id=oid, filled_shares=shares)
            state.inventory.on_fill(side_yes=side_yes, is_buy=True, shares=shares)
            state.fills_today += 1
            # Honest gate: a maker paper fill is a real executed trade, so
            # log it with reason=OK. This makes `python -m polybot.analyze`
            # and the dashboard reflect actual maker activity instead of
            # showing zero (maker fills bypass the taker execution engine
            # and never hit bot.db's orders table).
            emit_decision(
                cycle_id=f"maker-{int(time.time())}",
                strategy="maker_quoting",
                market_id=state.market.id,
                decision="BUY",
                reason=BlockReason.OK,
                mid=mid,
                size_usd=shares * price,
                extra={"side_yes": side_yes, "fill_price": price},
            )
            # Feed the fill into the per-market round-trip tracker. Only
            # when a fill *closes* against an earlier open lot do we record
            # a round-trip on the validation gate — the previous code
            # spammed the gate with gross=0 entries on every leg, which
            # inflated trade_count and never moved gross_pnl_usd.
            round_trips = state.round_trip_tracker.on_fill(
                side_yes=side_yes,
                is_buy=True,  # paper sweep only fills resting BUYs
                shares=shares,
                price=price,
            )
            if self._validation is not None:
                for rt in round_trips:
                    self._validation.record_round_trip(
                        gross_pnl_usd=rt.gross_pnl_usd,
                        fees_paid_usd=rt.fees_paid_usd,
                        dynamic_fee_fetched=True,
                    )

    # ─────────────────────────────────────────────────────────────────
    #  Vitals & dashboard surface
    # ─────────────────────────────────────────────────────────────────

    def vitals(self) -> MakerVitals:
        uptime = 0.0
        if self._loop_uptime_total > 0:
            uptime = 100.0 * (self._loop_synced_total / self._loop_uptime_total)
        net_inv = sum(s.inventory.net_yes_shares for s in self._states.values())
        fills = sum(s.fills_today for s in self._states.values())
        return MakerVitals(
            quote_uptime_pct=uptime,
            fills_today=fills,
            net_inventory_shares=net_inv,
            p95_latency_ms=self._latency.p95,
            active_markets=len(self._states),
            last_sync_action="loop_running" if self._running else "stopped",
            extras={
                "latency_breaching": self._latency.should_disable_live(),
            },
        )

    def _collect_vitals_for_heartbeat(self) -> HeartbeatVitals:
        v = self.vitals()
        return HeartbeatVitals(
            bankroll_usd=0.0,  # Bot fills this in via its own provider chain
            quote_uptime_pct=v.quote_uptime_pct,
            fills_today=v.fills_today,
            pnl_today_usd=0.0,
            net_inventory_shares=v.net_inventory_shares,
            p95_latency_ms=v.p95_latency_ms,
        )

    @property
    def latency_tracker(self) -> LatencyTracker:
        return self._latency

    @property
    def markets(self) -> dict[str, _MarketState]:
        return self._states

    @property
    def validation_gate(self) -> PaperValidationGate | None:
        return self._validation
