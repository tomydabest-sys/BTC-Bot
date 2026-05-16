"""Resting maker order lifecycle manager.

QuoteManager owns the placement, cancellation and replacement of every
resting maker order for a single market. It is the only module allowed
to touch the V2 batch order endpoint, which keeps the rest of the bot
free of timing-critical RPC code.

Operating model:
    1. Compute target quotes via MakerQuotingStrategy.
    2. Diff against current resting orders.
    3. Cancel-and-replace via the batch endpoint (≤15 ops per call).
    4. Time the round-trip; feed LatencyTracker.
    5. Emergency cancel-all on: WS disconnect, Binance staleness, risk
       trip, or LatencyTracker.should_disable_live().

In paper mode the V2 client is never called; we simulate immediate
acknowledgement and let the existing paper execution engine settle
fills out-of-band.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

import structlog

from polybot.api.clob_v2_client import ClobV2Client, V2OrderArgs
from polybot.api.rate_limits import BATCH_ORDER_MAX
from polybot.monitoring.latency_tracker import LatencyTracker
from polybot.strategies.maker_quoting import (
    MakerQuotingStrategy,
    NoQuote,
    Quote,
)

logger = structlog.get_logger()


@dataclass
class RestingOrder:
    order_id: str
    token_id: str
    price: float
    size: float
    side_yes: bool
    reference_fair: float
    created_at: float


@dataclass
class QuoteManagerConfig:
    stale_threshold_cents: float = 1.5
    max_quote_lifetime_s: float = 30.0
    flatten_before_expiry_s: float = 10.0
    binance_stale_threshold_s: float = 2.0
    clob_ws_reconnect_timeout_s: float = 5.0
    target_size_shares: float = 5.0


@dataclass
class QuoteManagerState:
    yes_token_id: str
    no_token_id: str
    market_id: str
    resting: dict[str, RestingOrder] = field(default_factory=dict)
    cancelled_at: float = 0.0
    last_sync_at: float = 0.0


class QuoteManager:
    """Single-market resting-order owner."""

    def __init__(
        self,
        *,
        market_id: str,
        yes_token_id: str,
        no_token_id: str,
        strategy: MakerQuotingStrategy,
        client: ClobV2Client | None,
        latency: LatencyTracker | None = None,
        config: QuoteManagerConfig | None = None,
        is_paper: bool = True,
    ) -> None:
        self._config = config or QuoteManagerConfig()
        self._strategy = strategy
        self._client = client
        self._latency = latency or LatencyTracker()
        self._is_paper = is_paper
        self._state = QuoteManagerState(
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            market_id=market_id,
        )
        self._lock = asyncio.Lock()

    @property
    def is_paper(self) -> bool:
        return self._is_paper

    @property
    def state(self) -> QuoteManagerState:
        return self._state

    @property
    def latency(self) -> LatencyTracker:
        return self._latency

    # ─────────────────────────────────────────────────────────────────
    #  Sync (the hot path)
    # ─────────────────────────────────────────────────────────────────

    async def sync_quotes(
        self,
        *,
        fair_value: float,
        fee_rate: float,
        vol_annual: float,
        time_remaining_s: float,
        inventory_skew_cents: float = 0.0,
        size_shares: float | None = None,
    ) -> dict[str, object]:
        """Recompute target quotes and (cancel+replace) into the book.

        Returns a small dict useful for the dashboard / decision log.
        """
        size = size_shares if size_shares is not None else self._config.target_size_shares
        # If we're past flatten threshold or the latency tracker has flipped,
        # caller should be calling flatten_all() instead. Defensive guard.
        if time_remaining_s <= self._config.flatten_before_expiry_s:
            return {"action": "flatten_window", "cancelled": await self.cancel_all()}
        if self._latency.should_disable_live():
            return {"action": "latency_kill", "cancelled": await self.cancel_all()}

        async with self._lock:
            try:
                yes_q, no_q = self._strategy.compute_quotes(
                    fair_value=fair_value,
                    fee_rate=fee_rate,
                    vol_annual=vol_annual,
                    time_remaining_s=time_remaining_s,
                    size_shares=size,
                    inventory_skew_cents=inventory_skew_cents,
                )
            except NoQuote as e:
                cancelled = await self._cancel_all_locked()
                return {"action": "no_quote", "reason": str(e), "cancelled": cancelled}

            to_cancel = self._stale_order_ids(fair_value, now=time.monotonic())
            to_place: list[V2OrderArgs] = []
            quote_meta: list[tuple[Quote, str]] = []  # (quote, token_id)
            for quote in (yes_q, no_q):
                if quote.size <= 0 or quote.price <= 0:
                    continue
                token_id = self._token_id_for(quote.side_yes)
                if self._already_resting(token_id, quote.price, quote.size):
                    continue
                to_place.append(
                    V2OrderArgs(
                        token_id=token_id,
                        price=quote.price,
                        size=quote.size,
                        side="BUY",
                        order_type="GTC",
                    )
                )
                quote_meta.append((quote, token_id))

            rtt_ms = await self._apply_batch_locked(to_cancel, to_place, quote_meta)
            self._latency.record(rtt_ms)
            self._state.last_sync_at = time.monotonic()
            return {
                "action": "synced",
                "cancelled": len(to_cancel),
                "placed": len(to_place),
                "rtt_ms": round(rtt_ms, 1),
                "p95_ms": round(self._latency.p95, 1),
            }

    # ─────────────────────────────────────────────────────────────────
    #  Emergency / lifecycle
    # ─────────────────────────────────────────────────────────────────

    async def cancel_all(self) -> int:
        async with self._lock:
            return await self._cancel_all_locked()

    async def _cancel_all_locked(self) -> int:
        if not self._state.resting:
            return 0
        ids = list(self._state.resting.keys())
        # Batch limit applies to cancellations too.
        for chunk_start in range(0, len(ids), BATCH_ORDER_MAX):
            chunk = ids[chunk_start : chunk_start + BATCH_ORDER_MAX]
            if not self._is_paper and self._client is not None:
                try:
                    await self._client.cancel_many(chunk)
                except Exception as e:
                    logger.error(
                        "quote_manager_cancel_failed",
                        market=self._state.market_id[:12],
                        err=str(e)[:80],
                    )
        self._state.resting.clear()
        self._state.cancelled_at = time.monotonic()
        return len(ids)

    async def on_feed_disconnect(self, *, reason: str) -> int:
        """Called by the main loop when Binance or CLOB WS goes silent."""
        logger.warning(
            "quote_manager_feed_drop",
            market=self._state.market_id[:12],
            reason=reason,
        )
        return await self.cancel_all()

    # ─────────────────────────────────────────────────────────────────
    #  Fill tracking — invoked by the fill listener
    # ─────────────────────────────────────────────────────────────────

    def on_fill(self, *, order_id: str, filled_shares: float) -> RestingOrder | None:
        """Mark a resting order partially/fully consumed; return it for
        upstream inventory bookkeeping."""
        order = self._state.resting.get(order_id)
        if order is None:
            return None
        order.size = max(0.0, order.size - filled_shares)
        if order.size <= 0:
            self._state.resting.pop(order_id, None)
        return order

    # ─────────────────────────────────────────────────────────────────
    #  Internals
    # ─────────────────────────────────────────────────────────────────

    def _token_id_for(self, side_yes: bool) -> str:
        return self._state.yes_token_id if side_yes else self._state.no_token_id

    def _already_resting(self, token_id: str, price: float, size: float) -> bool:
        for o in self._state.resting.values():
            if (
                o.token_id == token_id
                and abs(o.price - price) < 1e-9
                and abs(o.size - size) < 1e-9
            ):
                return True
        return False

    def _stale_order_ids(self, fair_value: float, *, now: float) -> list[str]:
        stale: list[str] = []
        for oid, order in self._state.resting.items():
            if self._strategy.is_quote_stale(
                quote_reference_fair=order.reference_fair,
                current_fair=fair_value,
                threshold_cents=self._config.stale_threshold_cents,
            ):
                stale.append(oid)
                continue
            if (now - order.created_at) >= self._config.max_quote_lifetime_s:
                stale.append(oid)
        return stale

    async def _apply_batch_locked(
        self,
        to_cancel: list[str],
        to_place: list[V2OrderArgs],
        quote_meta: list[tuple[Quote, str]],
    ) -> float:
        """Execute the cancel+place batch; return RTT in milliseconds."""
        if not to_cancel and not to_place:
            return 0.0

        start = time.monotonic()
        if self._is_paper or self._client is None:
            self._paper_apply(to_cancel, to_place, quote_meta, start=start)
            return 5.0

        try:
            await self._live_cancel(to_cancel)
            await self._live_place(to_place, quote_meta, start=start)
        except Exception as e:
            logger.error(
                "quote_manager_batch_failed",
                market=self._state.market_id[:12],
                err=str(e)[:120],
            )
        return (time.monotonic() - start) * 1000.0

    def _paper_apply(
        self,
        to_cancel: list[str],
        to_place: list[V2OrderArgs],
        quote_meta: list[tuple[Quote, str]],
        *,
        start: float,
    ) -> None:
        for oid in to_cancel:
            self._state.resting.pop(oid, None)
        for (quote, token_id), args in zip(quote_meta, to_place, strict=True):
            oid = f"paper-{uuid.uuid4().hex[:12]}"
            self._state.resting[oid] = RestingOrder(
                order_id=oid,
                token_id=token_id,
                price=args.price,
                size=args.size,
                side_yes=quote.side_yes,
                reference_fair=quote.reference_fair,
                created_at=start,
            )

    async def _live_cancel(self, to_cancel: list[str]) -> None:
        if not to_cancel or self._client is None:
            return
        for chunk_start in range(0, len(to_cancel), BATCH_ORDER_MAX):
            chunk = to_cancel[chunk_start : chunk_start + BATCH_ORDER_MAX]
            await self._client.cancel_many(chunk)
            for oid in chunk:
                self._state.resting.pop(oid, None)

    async def _live_place(
        self,
        to_place: list[V2OrderArgs],
        quote_meta: list[tuple[Quote, str]],
        *,
        start: float,
    ) -> None:
        if not to_place or self._client is None:
            return
        for chunk_start in range(0, len(to_place), BATCH_ORDER_MAX):
            chunk = to_place[chunk_start : chunk_start + BATCH_ORDER_MAX]
            chunk_meta = quote_meta[chunk_start : chunk_start + BATCH_ORDER_MAX]
            responses = await self._client.post_orders(chunk)
            for resp, (quote, token_id), args in zip(
                responses, chunk_meta, chunk, strict=True
            ):
                oid = str(
                    resp.get("orderID")
                    or resp.get("transactionID")
                    or uuid.uuid4().hex
                )
                self._state.resting[oid] = RestingOrder(
                    order_id=oid,
                    token_id=token_id,
                    price=args.price,
                    size=args.size,
                    side_yes=quote.side_yes,
                    reference_fair=quote.reference_fair,
                    created_at=start,
                )
