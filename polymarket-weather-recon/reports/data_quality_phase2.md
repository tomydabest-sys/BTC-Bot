# Phase 2 — data-quality report (bounded window)

Generated: 2026-05-29T23:55:24.475203+00:00

## Coverage
- Markets discovered: **3201**
- Markets with ≥1 trade: **3201** (zero-trade: 0)
- Trades ingested: **1793951**
- USDC volume (Σ size×price): **24,886,788.59**
- Unique wallets (taker-side): **38246**
- Date range: 2026-03-27T10:07:35+00:00 → 2026-05-29T23:51:31+00:00
- Price range observed: 0.001 – 0.999000000999 (sanity: must be within [0,1])
- Side split: {'BUY': 1042261, 'SELL': 751690}

## Honesty / known limits
- **Taker-side only.** The Data API exposes one wallet per fill; the maker counterparty is not available (subgraph/on-chain blocked). `side_split` is taker BUY/SELL, NOT maker-vs-taker.
- **No on-chain reconciliation** under the current allowlist; completeness is argued from internal consistency only.

## Possible truncation (verify)
Markets whose ingested count is an exact multiple of 250 (the page cap) may be truncated by the Data API's pagination limit:
- `0x76ef6722166c95b9a1a8192a9897844004d436d69fc3e1d70089086e7eb12033` — 3250 rows
- `0x1f8f15deed45b475573a8bdd2c4ba1f965860e88af18e5d1be66269097cdc966` — 750 rows
- `0xf62571ed81fdadd1fe06e1537f46be8ca8a76869cfa49ccc88fa479059f64311` — 250 rows
- `0xe0c1a994d9db13fbeb26e6b67778b8319bc83645050db74f08cdf0b424a7fd33` — 3250 rows
- `0x7c1ac87461b1c76c00c28641446abf76d5a19444d2e7f0b08d0d86256ee8a702` — 3250 rows
- `0x975e7298d48075454c7ae3c108d5c35b0ce5f426cfc357630d63db164a32dd8c` — 3250 rows
- `0x9d57fd86e9d4a64a2f7fcb9d8ffd19fda1b0ae138ea08db03cab24537e440ae8` — 2250 rows
- `0x8e6cca821a711ee75670dcdd659e40d278a53ac294e0187359a4c4742a8b6f8b` — 1500 rows
- `0xd1cd4712ce45c3418d00399d168a09596b639eb65039734c66420a01015e7543` — 250 rows
- `0x7d1674a247d987acc6408cd83da31822da3b163508e8b2e6a39a30dd9df121e4` — 250 rows
- `0x8070db5ab11bf1cbf94da073b56567197a2b516cca98d1a03e97be350974ea65` — 1000 rows
- `0x752c7b7e18e4f19d0010b7106b089090efcd0c30344f68ff5df6ad2305ff64b0` — 1000 rows

## Top markets by trade count

| trades | usdc | question |
|--:|--:|---|
| 3250 | 50732.47 | Will the highest temperature in New York City be 74°F or higher on March 31? |
| 3250 | 94596.21 | Will the highest temperature in London be 17°C on April 7? |
| 3250 | 41558.67 | Will the highest temperature in London be 18°C on April 7? |
| 3250 | 59979.6 | Will the highest temperature in New York City be 77°F or below on April 17? |
| 3044 | 24962.72 | Will the highest temperature in London be 19°C on April 7? |
| 2846 | 141331.13 | Will the highest temperature in Paris be 18°C on April 15? |
| 2776 | 48504.31 | Will the highest temperature in London be 13°C on April 13? |
| 2742 | 36085.0 | Will the highest temperature in London be 15°C on April 4? |
| 2686 | 23086.97 | Will the highest temperature in New York City be between 64-65°F on April 3? |
| 2626 | 28094.49 | Will the highest temperature in New York City be between 80-81°F on April 17? |
