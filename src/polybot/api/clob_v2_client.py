"""CLOB V2 client wrapper.

Encapsulates the V2 SDK so the rest of the bot never imports it directly.

Why wrap?

* The brief is explicit that V2 *removed* `feeRateBps`, `nonce` and `taker`
  from order construction. Centralising order-building here means a
  forgotten field can never leak into a strategy.
* Fees are now dynamic per-market and must be fetched live; a 30s TTL
  cache fronts `/fee-rate` so high-frequency quoting doesn't melt the
  rate limiter.
* V2 uses pUSD as collateral (USDC.e on Polygon under the hood). Balance
  reads go through one place so unit conversion errors don't propagate.
* The V2 SDK package (`py-clob-client-v2`) may not be installed in
  paper-only environments. We import lazily and only when a method that
  actually needs the live SDK is called.

This module never *executes* an order while `LIVE_TRADING_ENABLED` is
False — the paper engine bypasses it entirely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from polybot.api.rate_limits import BATCH_ORDER_MAX, MultiBucketLimiter

logger = structlog.get_logger()


CLOB_BASE_URL = "https://clob.polymarket.com"
DEFAULT_FEE_CACHE_TTL_S = 30.0


class V2SDKNotInstalledError(RuntimeError):
    """Raised when a live-only V2 SDK call is attempted without the SDK."""


# Backwards-compatible alias used in early test scaffolding.
V2SDKNotInstalled = V2SDKNotInstalledError


@dataclass
class _FeeEntry:
    fee_rate: float
    fetched_at: float


@dataclass
class V2OrderArgs:
    """Plain-data order spec, free of V1 fields.

    The V2 SDK accepts a `OrderArgs` object; we mirror it locally so the
    rest of the bot can construct orders without depending on the SDK
    being importable. The wrapper translates to the SDK type at the last
    possible moment.
    """

    token_id: str
    price: float
    size: float
    side: str  # "BUY" | "SELL"
    order_type: str = "GTC"  # "GTC" | "FOK" | "FAK"
    expiration: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        side = self.side.upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"V2OrderArgs: bad side {self.side!r}")
        self.side = side
        ot = self.order_type.upper()
        if ot not in {"GTC", "FOK", "FAK"}:
            raise ValueError(f"V2OrderArgs: bad order_type {self.order_type!r}")
        self.order_type = ot
        if not (0.0 < self.price < 1.0):
            raise ValueError(f"V2OrderArgs: price {self.price!r} not in (0, 1)")
        if self.size <= 0:
            raise ValueError(f"V2OrderArgs: size {self.size!r} must be > 0")
        # Forbidden V1 fields — fail loudly if someone copies legacy code.
        for banned in ("feeRateBps", "nonce", "taker"):
            if banned in self.extra:
                raise ValueError(
                    f"V2OrderArgs: '{banned}' is a V1-only field and must not be set"
                )


class ClobV2Client:
    """Thin async wrapper around the V2 SDK + REST surface.

    Construction is cheap (no network calls). Call `start()` before use so
    the httpx client is initialised. Use `await ...` for everything.
    """

    def __init__(
        self,
        *,
        host: str = CLOB_BASE_URL,
        chain_id: int = 137,
        private_key: str | None = None,
        api_creds: dict[str, str] | None = None,
        fee_cache_ttl_s: float = DEFAULT_FEE_CACHE_TTL_S,
        limiter: MultiBucketLimiter | None = None,
    ) -> None:
        self._host = host.rstrip("/")
        self._chain_id = chain_id
        self._private_key = private_key
        self._api_creds = api_creds or {}
        self._fee_cache: dict[str, _FeeEntry] = {}
        self._fee_cache_ttl_s = fee_cache_ttl_s
        self._limiter = limiter or MultiBucketLimiter.from_defaults()
        self._http: httpx.AsyncClient | None = None
        self._sdk_client: Any | None = None  # py_clob_client_v2.ClobClient

    # ─────────────────────────────────────────────────────────────────
    #  Lifecycle
    # ─────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self._host, timeout=15.0)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ─────────────────────────────────────────────────────────────────
    #  SDK access (lazy)
    # ─────────────────────────────────────────────────────────────────

    def _ensure_sdk(self) -> Any:
        if self._sdk_client is not None:
            return self._sdk_client
        try:
            # Imported lazily so paper mode works without the SDK installed.
            from py_clob_client_v2.client import ClobClient  # type: ignore[import-not-found]
        except Exception as e:
            raise V2SDKNotInstalledError(
                "py-clob-client-v2 is required for live trading. "
                "Install it with `pip install py-clob-client-v2` and "
                "ensure LIVE_TRADING_ENABLED is True."
            ) from e

        if not self._private_key:
            raise V2SDKNotInstalledError(
                "ClobV2Client: private key not supplied; cannot construct "
                "live SDK client."
            )

        client = ClobClient(
            host=self._host,
            chain_id=self._chain_id,
            key=self._private_key,
        )
        if self._api_creds:
            try:
                client.set_api_creds(self._api_creds)  # type: ignore[attr-defined]
            except Exception:
                pass
        self._sdk_client = client
        return client

    # ─────────────────────────────────────────────────────────────────
    #  Fee rate (live; cached)
    # ─────────────────────────────────────────────────────────────────

    async def get_fee_rate(self, token_id: str) -> float:
        """Return the V2 fee rate for `token_id`. Caches for 30s by default.

        Polymarket has changed fee parameters without notice in 2026, so
        every order-signing path MUST call this rather than hardcoding.
        """
        now = time.monotonic()
        entry = self._fee_cache.get(token_id)
        if entry and (now - entry.fetched_at) < self._fee_cache_ttl_s:
            return entry.fee_rate

        if self._http is None:
            await self.start()
        assert self._http is not None

        await self._limiter.acquire("clob_general")
        try:
            resp = await self._http.get(
                "/fee-rate", params={"token_id": token_id}
            )
            resp.raise_for_status()
            data = resp.json()
            # Endpoint shape: {"feeRate": "0.072", ...} — be liberal.
            raw = data.get("feeRate", data.get("fee_rate"))
            fee = float(raw) if raw is not None else 0.0
        except Exception as e:
            logger.warning(
                "v2_fee_rate_fetch_failed",
                token_id=token_id[:12],
                err=str(e)[:100],
            )
            # Fall back to last known value if we have one, otherwise the
            # documented crypto theta.
            if entry is not None:
                return entry.fee_rate
            fee = 0.072

        self._fee_cache[token_id] = _FeeEntry(fee_rate=fee, fetched_at=now)
        return fee

    def fee_cache_snapshot(self) -> dict[str, float]:
        return {tid: e.fee_rate for tid, e in self._fee_cache.items()}

    # ─────────────────────────────────────────────────────────────────
    #  Order placement
    # ─────────────────────────────────────────────────────────────────

    async def post_order(self, args: V2OrderArgs) -> dict[str, Any]:
        """Sign + submit a single order.

        V2 returns `{"transactionID": ..., "state": "STATE_NEW"}` straight
        away — there is no `transactionHash` to wait on. Callers should
        treat `STATE_NEW` as 'accepted, not yet on chain' and poll if they
        need confirmation.
        """
        # Refresh the fee rate before signing so we never use a stale value.
        await self.get_fee_rate(args.token_id)
        await self._limiter.acquire("post_order")
        sdk = self._ensure_sdk()
        from py_clob_client_v2.clob_types import (  # type: ignore[import-not-found]
            OrderArgs,
            OrderType,
        )

        order_args = OrderArgs(
            token_id=args.token_id,
            price=args.price,
            size=args.size,
            side=args.side,
        )
        order_type = getattr(OrderType, args.order_type)
        resp = sdk.create_and_post_order(order_args, order_type=order_type)
        return self._normalise_post_response(resp)

    async def post_orders(
        self, batch: list[V2OrderArgs]
    ) -> list[dict[str, Any]]:
        """Submit up to `BATCH_ORDER_MAX` orders in one batch call."""
        if not batch:
            return []
        if len(batch) > BATCH_ORDER_MAX:
            raise ValueError(
                f"V2 batch limit is {BATCH_ORDER_MAX}; got {len(batch)}"
            )
        for args in batch:
            await self.get_fee_rate(args.token_id)
        await self._limiter.acquire("batch")
        sdk = self._ensure_sdk()
        from py_clob_client_v2.clob_types import (  # type: ignore[import-not-found]
            OrderArgs,
            OrderType,
        )

        sdk_args = [
            (
                OrderArgs(
                    token_id=a.token_id,
                    price=a.price,
                    size=a.size,
                    side=a.side,
                ),
                getattr(OrderType, a.order_type),
            )
            for a in batch
        ]
        resp = sdk.create_and_post_orders(sdk_args)
        if not isinstance(resp, list):
            return [self._normalise_post_response(resp)]
        return [self._normalise_post_response(r) for r in resp]

    async def cancel(self, order_id: str) -> dict[str, Any]:
        await self._limiter.acquire("delete_order")
        sdk = self._ensure_sdk()
        return sdk.cancel(order_id)  # type: ignore[no-any-return]

    async def cancel_many(self, order_ids: list[str]) -> dict[str, Any]:
        if not order_ids:
            return {}
        if len(order_ids) > BATCH_ORDER_MAX:
            raise ValueError(
                f"V2 batch cancel limit is {BATCH_ORDER_MAX}; got {len(order_ids)}"
            )
        await self._limiter.acquire("batch")
        sdk = self._ensure_sdk()
        return sdk.cancel_orders(order_ids)  # type: ignore[no-any-return]

    # ─────────────────────────────────────────────────────────────────
    #  Helpers
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalise_post_response(resp: Any) -> dict[str, Any]:
        """V2 SDK responses are dict-like; just defensively coerce."""
        if isinstance(resp, dict):
            out = dict(resp)
        else:
            out = {"raw": resp}
        # V1 callers used to wait on `transactionHash`; V2 returns
        # `transactionID` + `state`. Surface both so older callers don't
        # silently see `None` forever.
        out.setdefault("transactionID", out.get("transactionId"))
        out.setdefault("state", out.get("state", "STATE_NEW"))
        return out
