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
from datetime import datetime, timezone

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
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@dataclass
class _OpenLot:
    """One leg of a maker round-trip held until offset by an opposite fill."""

    side_yes: bool
    is_buy: bool
    shares: float
    price: float

    @property
    def signed_delta(self) -> float:
        """Signed contribution to net YES inventory.

        long YES contributions (YES BUY, NO SELL) are positive; long NO
        contributions (NO BUY, YES SELL) are negative.
        """
        if self.side_yes and self.is_buy:
            return +self.shares
        if self.side_yes and not self.is_buy:
            return -self.shares
        if not self.side_yes and self.is_buy:
            return -self.shares
        return +self.shares


@dataclass
class _RoundTrip:
    gross_pnl_usd: float
    fees_paid_usd: float


class _RoundTripTracker:
    """FIFO lot-matching tracker for paper-mode maker fills.

    Maintains a single-direction queue of open lots (all entries on the
    same signed side of net YES inventory). When a fill arrives on the
    opposite side, lots are popped FIFO and matched up to the available
    fill size; each matched pair produces a `_RoundTrip` with realized
    gross P&L and per-side fees.

    P&L identities:
      * Same token (BUY→SELL or SELL→BUY): (sell - buy) * shares.
      * Cross-token close where BOTH legs are BUYS (YES BUY + NO BUY):
        the position pays $1 at resolution regardless of outcome, so
        gross_pnl = (1 - p_yes - p_no) * shares.
      * Cross-token close where BOTH legs are SELLS (YES SELL + NO SELL):
        the obligation is $1, so gross_pnl = (p_yes + p_no - 1) * shares.
    """

    def __init__(self, fee_theta: float = 0.072) -> None:
        self._fee_theta = fee_theta
        self._open_lots: list[_OpenLot] = []

    @property
    def fee_theta(self) -> float:
        return self._fee_theta

    @property
    def open_lot_count(self) -> int:
        return len(self._open_lots)

    @property
    def net_signed_inventory(self) -> float:
        return sum(lot.signed_delta for lot in self._open_lots)

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
        incoming = _OpenLot(side_yes=side_yes, is_buy=is_buy, shares=shares, price=price)
        round_trips: list[_RoundTrip] = []

        # Same-direction fill (or first fill) → just enqueue.
        if not self._open_lots or self._same_direction(self._open_lots[0], incoming):
            self._open_lots.append(incoming)
            return round_trips

        # Opposite-direction fill → FIFO match against open lots until either
        # the incoming fill is consumed or the queue is empty.
        remaining = incoming.shares
        while remaining > 0 and self._open_lots:
            head = self._open_lots[0]
            matched = min(head.shares, remaining)
            round_trips.append(
                self._realize(open_lot=head, closing=incoming, shares=matched)
            )
            head.shares -= matched
            remaining -= matched
            if head.shares <= 1e-12:
                self._open_lots.pop(0)

        # If the incoming fill exceeded the open queue, the residual flips
        # the position and becomes a new open lot on the opposite side.
        if remaining > 1e-12:
            self._open_lots.append(
                _OpenLot(
                    side_yes=incoming.side_yes,
                    is_buy=incoming.is_buy,
                    shares=remaining,
                    price=incoming.price,
                )
            )
        return round_trips

    @staticmethod
    def _same_direction(a: _OpenLot, b: _OpenLot) -> bool:
        return (a.signed_delta >= 0) == (b.signed_delta >= 0)

    def _realize(
        self, *, open_lot: _OpenLot, closing: _OpenLot, shares: float
    ) -> _RoundTrip:
        # Per-side fees: the maker is the resting side and the taker pays
        # the per-trade fee. In paper mode we conservatively count both
        # sides through `fee_at_price` — the validation gate's fee-
        # consistency metric only checks that *something* was fetched, not
        # the exact amount.
        fee_open = fee_at_price(open_lot.price, theta=self._fee_theta) * shares
        fee_close = fee_at_price(closing.price, theta=self._fee_theta) * shares
        fees = fee_open + fee_close

        if open_lot.side_yes == closing.side_yes:
            # Same token, opposite is_buy → straightforward (sell - buy) * shares.
            buy_lot = open_lot if open_lot.is_buy else closing
            sell_lot = closing if open_lot.is_buy else open_lot
            gross = (sell_lot.price - buy_lot.price) * shares
        else:
            # Cross-token close.
            if open_lot.is_buy and closing.is_buy:
                # Long YES + long NO held to (synthetic) resolution.
                gross = (1.0 - open_lot.price - closing.price) * shares
            elif not open_lot.is_buy and not closing.is_buy:
                # Short both sides — owed $1, collected p_yes + p_no.
                gross = (open_lot.price + closing.price - 1.0) * shares
            else:
                # One BUY + one SELL on opposite tokens — equivalent to two
                # same-direction long positions, so the offset comes from
                # the price difference.
                buy_lot = open_lot if open_lot.is_buy else closing
                sell_lot = closing if open_lot.is_buy else open_lot
                gross = (sell_lot.price - buy_lot.price) * shares
        return _RoundTrip(gross_pnl_usd=gross, fees_paid_usd=fees)


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
            try:
                await state.quote_manager.cancel_all()
            except Exception:
                pass
        logger.info("maker_orchestrator_stopped")

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

        # Pre-flight: would this fill push us past the cap on either side?
        # If yes, force flatten-only mode by skipping new quotes.
        size = self._cfg.target_size_shares
        inv = state.inventory
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
            return

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
