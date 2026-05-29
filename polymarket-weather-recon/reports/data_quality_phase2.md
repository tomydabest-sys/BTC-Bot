# Phase 2 — data-quality report (bounded window)

Generated: 2026-05-29T11:19:56.005597+00:00

## Coverage
- Markets discovered: **319**
- Markets with ≥1 trade: **319** (zero-trade: 0)
- Trades ingested: **159920**
- USDC volume (Σ size×price): **2,388,775.2**
- Unique wallets (taker-side): **9095**
- Date range: 2026-04-27T04:14:28+00:00 → 2026-05-29T11:19:27+00:00
- Price range observed: 0.001 – 0.9990000002999 (sanity: must be within [0,1])
- Side split: {'BUY': 100770, 'SELL': 59150}

## Honesty / known limits
- **Taker-side only.** The Data API exposes one wallet per fill; the maker counterparty is not available (subgraph/on-chain blocked). `side_split` is taker BUY/SELL, NOT maker-vs-taker.
- **No on-chain reconciliation** under the current allowlist; completeness is argued from internal consistency only.

## Possible truncation (verify)
Markets whose ingested count is an exact multiple of 250 (the page cap) may be truncated by the Data API's pagination limit:
- none — no market hit an exact 250-multiple boundary.

## Top markets by trade count

| trades | usdc | question |
|--:|--:|---|
| 2152 | 18611.11 | Will the highest temperature in New York City be between 78-79°F on May 5? |
| 2143 | 111236.19 | Will the highest temperature in New York City be between 62-63°F on May 6? |
| 2012 | 32759.07 | Will the highest temperature in New York City be between 66-67°F on May 6? |
| 1967 | 114641.4 | Will the highest temperature in New York City be between 60-61°F on May 6? |
| 1713 | 30658.58 | Will the highest temperature in New York City be between 70-71°F on May 4? |
| 1704 | 29041.79 | Will the highest temperature in New York City be between 60-61°F on May 11? |
| 1645 | 36068.79 | Will the highest temperature in New York City be between 76-77°F on May 5? |
| 1610 | 33847.46 | Will the highest temperature in New York City be between 76-77°F on May 16? |
| 1583 | 20512.47 | Will the highest temperature in New York City be between 70-71°F on May 6? |
| 1577 | 14715.45 | Will the highest temperature in New York City be between 68-69°F on May 6? |
