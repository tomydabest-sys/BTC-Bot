# Phase 4 — bot detection, clustering & operator grouping

Wallets total: **9095** | scored (≥30 trades): **748** | bot_score≥0.5: **191** | ≥0.7: **17**

## Heuristic score (transparent, weighted; see config weights)
Components: cadence regularity, 24/7 activity, size regularity, breadth, burstiness, reaction alignment. Wallets <min trades are 'unscored'.

## Clustering cross-check
```
{'features': ['cv_gap', 'hour_entropy', 'active_hours', 'breadth_markets', 'mode_size_frac', 'max_trades_per_sec', 'extreme_price_frac', 'reaction_alignment'], 'n_scored': 748, 'hdbscan_clusters': 3, 'hdbscan_noise': 94, 'kmeans_k': 2, 'kmeans_silhouette': 0.404}
```

HDBSCAN found 3 crisp density cluster(s) (94 noise) — the scored population is largely a continuum, so the profile below uses the **KMeans** cross-check (k=2, silhouette 0.404).

### Cluster profiles by `kmeans` (median features)

|   kmeans |   cv_gap |   hour_entropy |   active_hours |   breadth_markets |   mode_size_frac |   max_trades_per_sec |   extreme_price_frac |   reaction_alignment |   bot_score |   n_wallets |
|---------:|---------:|---------------:|---------------:|------------------:|-----------------:|---------------------:|---------------------:|---------------------:|------------:|------------:|
|        0 |    3.345 |          4.134 |             22 |               129 |            0.534 |                    9 |                0.67  |                0.674 |       0.618 |          73 |
|        1 |    2.427 |          3.373 |             14 |                24 |            0.101 |                    2 |                0.257 |                0.469 |       0.429 |         675 |

Agreement check: the cluster with high median bot_score should match the high-scoring heuristic population (raises confidence); per-wallet labels are in wallet_classification.csv.

## Operator grouping (co-timing; on-chain funding unavailable)
Multi-wallet operators detected: **10** (covering 198 wallets).

> ⚠️ **Over-merge caveat:** transitive union-find (A~B, B~C ⇒ A~C) can fuse a densely co-active bot population into one giant component. 1 operator(s) have >30 wallets and likely represent a co-trading *cluster / shared infra*, not a single controller. The small (2–15 wallet) components are the more credible same-operator groups.

| operator | wallets |
|---|--:|
| op_001 | 146 |
| op_003 | 15 |
| op_006 | 12 |
| op_009 | 5 |
| op_002 | 4 |
| op_004 | 4 |
| op_010 | 4 |
| op_008 | 3 |
| op_007 | 3 |
| op_005 | 2 |

## Top 15 by bot_score

| wallet | bot_score | n_trades | active_h | breadth | max/s | react | realized_pnl | roi | win |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 0xc0d89633… | 0.74 | 1500 | 24 | 165 | 10 | 0.78 | 41 | 0.046 | 0.88 |
| 0x900939fa… | 0.74 | 2098 | 24 | 188 | 18 | 0.78 | 128 | 0.110 | 0.89 |
| 0xc90b20dd… | 0.74 | 1798 | 24 | 172 | 14 | 0.76 | 83 | 0.081 | 0.89 |
| 0x04dcf8d1… | 0.73 | 3813 | 24 | 209 | 20 | 0.77 | 234 | 0.121 | 0.90 |
| 0x20d81d32… | 0.73 | 953 | 22 | 146 | 10 | 0.79 | 46 | 0.078 | 0.88 |
| 0x19af9895… | 0.73 | 1143 | 22 | 154 | 10 | 0.77 | 44 | 0.064 | 0.88 |
| 0xcb329347… | 0.73 | 2776 | 24 | 206 | 20 | 0.74 | 166 | 0.115 | 0.90 |
| 0xe43ff9e3… | 0.72 | 1167 | 22 | 155 | 10 | 0.76 | 47 | 0.068 | 0.88 |
| 0xd009eeee… | 0.72 | 1299 | 23 | 162 | 18 | 0.72 | 38 | 0.049 | 0.88 |
| 0x23d18416… | 0.72 | 5568 | 24 | 224 | 20 | 0.68 | 268 | 0.097 | 0.90 |
| 0xa5cbba9e… | 0.71 | 678 | 20 | 123 | 10 | 0.77 | 34 | 0.079 | 0.88 |
| 0x8c8c6ff6… | 0.71 | 574 | 20 | 117 | 9 | 0.79 | 26 | 0.068 | 0.87 |
| 0x19030321… | 0.71 | 569 | 20 | 118 | 9 | 0.76 | 32 | 0.088 | 0.88 |
| 0x486f33c4… | 0.71 | 578 | 20 | 116 | 10 | 0.77 | 39 | 0.098 | 0.87 |
| 0xd2bbc6ed… | 0.71 | 685 | 19 | 126 | 10 | 0.78 | 49 | 0.114 | 0.88 |

## Honesty / confidence
- Taker-side only; maker-fraction & maker adverse-selection absent.
- Reaction alignment is COARSE (hourly reference) — low/medium confidence.
- Operator grouping is co-timing-based (medium confidence; not proof of control).
- Fixed-offset pricing component deferred (no robust historical mid).
