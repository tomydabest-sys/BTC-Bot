"""Phase 2 — PRIMARY trade ingestion via the Polymarket Data API (STUB).

Under the current network allowlist the Goldsky subgraph and Polygon RPC are
BLOCKED, so the Data API is the de-facto primary trade source. Implement after
the Phase 0 checkpoint.

Verified Phase-0 facts (see FINDINGS.md):
  * GET {data_api}/trades?market=<conditionId>&limit=250&offset=N
      - `market` param takes a CONDITION ID (not token id).
      - rows cap at 250/request regardless of a higher limit.
      - paginate offset in steps of 250 until a page returns 0 rows; treat tiny
        non-empty pages at very large offsets as artifacts (ignore).
  * Each row = ONE wallet's fill (taker side): fields proxyWallet, side
    (BUY/SELL), asset (token id), conditionId, size (SHARES, human decimals),
    price ([0,1]), timestamp (unix s), transactionHash, outcome, outcomeIndex.
  * MAKER COUNTERPARTY IS NOT EXPOSED. One row per tx hash. The brief's
    "model both maker and taker as first-class" is NOT satisfiable from this
    source alone (needs the blocked subgraph/on-chain). Model what we have:
    wallet-attributed taker-side fills. Record this limitation in the data
    table (e.g. side_attribution = 'taker_only').
  * /activity?user=<addr> adds usdcSize + type; /positions gives
    avgPrice/realizedPnl/cashPnl (PnL proxy); /holders gives holder snapshots.
  * USDC notional = size * price (Data API decimals are already human-readable;
    do NOT divide by 1e6).

Completeness caveat to validate in Phase 2: confirm per-market trade counts are
not silently truncated (cross-check against /positions aggregates and Gamma
volume). Guaranteed completeness on high-volume markets needs the subgraph/
on-chain, which are currently unavailable.
"""
from __future__ import annotations


def ingest_market_trades(condition_id: str, cache_bust: bool = False):
    raise NotImplementedError("Phase 2 — pending Phase 0 checkpoint sign-off")
