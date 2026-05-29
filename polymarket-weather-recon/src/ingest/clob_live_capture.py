"""CLOB price/book capture (STUB).

Two roles:
  * Historical (supports Phase 3/6): GET {clob}/prices-history?market=<tokenId>
    &startTs=..&endTs=..&fidelity=<minutes> returns a coarse price series even
    for CLOSED markets (point density tracks activity). Used to approximate the
    "prevailing mid" since full order-book history is unavailable. Note:
    interval=1m is invalid (400); interval=max+fidelity works for ACTIVE tokens.
  * Forward live capture (Phase 8, OPTIONAL, only on explicit go-ahead):
    snapshot /book + /midpoint + /price at a fixed cadence to observe maker
    quote/cancel churn that historical data cannot recover.

NOTE on the critical data gap: order placements and cancellations live in the
off-chain CLOB matching engine and appear in NO historical source. Maker quote
churn / rebate-harvesting is observable ONLY via forward live capture.
"""
from __future__ import annotations


def fetch_price_history(token_id: str, start_ts: int, end_ts: int, fidelity: int = 60):
    raise NotImplementedError("Phase 3/6 helper — pending checkpoint sign-off")


def capture_live_book(token_ids, cache_bust: bool = False):
    raise NotImplementedError("Phase 8 — OPTIONAL, only on explicit go-ahead")
