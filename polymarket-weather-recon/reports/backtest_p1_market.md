# P1 variant — market-price-triggered resolution-drift backtest

No weather feed. Trigger: a bucket's YES price crosses **θ** on the resolution day; enter at that YES print (trade-stream proxy), hold to resolution. Causal — outcome used only for PnL. **NYC = in-sample; London/Paris/Chicago/Miami = OUT-OF-SAMPLE.**

## OUT-OF-SAMPLE (London + Paris + Chicago + Miami) — the real test

| θ | entries | hit rate | avg entry | avg edge/share | deployed$ | PnL(fee0) | ROI(fee0) | ROI(fee 0.02) |
|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 0.5 | 412 | 0.55 | 0.568 | -0.0165 | 19,982 | 1 | 0.000 | -0.036 |
| 0.6 | 365 | 0.62 | 0.662 | -0.0433 | 17,797 | -770 | -0.043 | -0.074 |
| 0.7 | 315 | 0.72 | 0.755 | -0.0372 | 15,212 | -302 | -0.020 | -0.047 |
| 0.8 | 282 | 0.80 | 0.845 | -0.0438 | 13,756 | -447 | -0.033 | -0.056 |
| 0.9 | 255 | 0.89 | 0.924 | -0.0379 | 12,539 | -357 | -0.029 | -0.050 |
| 0.95 | 241 | 0.94 | 0.959 | -0.0216 | 11,924 | -187 | -0.016 | -0.037 |

## In-sample (NYC) — for comparison

| θ | entries | hit rate | avg entry | avg edge/share | deployed$ | PnL(fee0) | ROI(fee0) | ROI(fee 0.02) |
|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 0.5 | 106 | 0.54 | 0.557 | -0.0189 | 5,170 | 49 | 0.009 | -0.028 |
| 0.6 | 91 | 0.63 | 0.662 | -0.0361 | 4,390 | -62 | -0.014 | -0.045 |
| 0.7 | 75 | 0.76 | 0.756 | +0.0041 | 3,641 | 163 | 0.045 | 0.018 |
| 0.8 | 66 | 0.86 | 0.842 | +0.0213 | 3,195 | 210 | 0.066 | 0.042 |
| 0.9 | 62 | 0.92 | 0.919 | +0.0001 | 3,018 | 88 | 0.029 | 0.007 |
| 0.95 | 59 | 0.95 | 0.958 | -0.0087 | 2,913 | 11 | 0.004 | -0.017 |

## Per-city @ θ=0.8

| city | entries | hit rate | avg entry | ROI(fee0) | ROI(fee 0.02) |
|---|--:|--:|--:|--:|--:|
| Chicago | 65 | 0.86 | 0.893 | -0.024 | -0.047 |
| London | 75 | 0.76 | 0.833 | -0.066 | -0.090 |
| Miami | 68 | 0.84 | 0.830 | 0.032 | 0.007 |
| New York City | 66 | 0.86 | 0.842 | 0.066 | 0.042 |
| Paris | 74 | 0.76 | 0.830 | -0.065 | -0.089 |

## Verdict
- Out-of-sample @ θ=0.8: ROI **-0.0325** (fee0) / **-0.0563** (fee 0.02), hit 0.8014, n=282.
- Best out-of-sample θ by ROI(fee0): 0.0001 (hit 0.551, entry 0.5675).
- **The θ≈0.8 edge DOES NOT generalise out-of-sample after a stressed fee.** An edge present in every city (per-city table) is far more trustworthy than one driven by one market.

## Caveats
- Trade-stream proxy (no historical book): assumes the print is takeable with no queue/slippage and ignores our own market impact — **optimistic**.
- ~58-day window per city; still a backtest, not live. Per-entry capacity capped at $50.
- A faster *weather* signal (NWS live) would let you enter before full price convergence, improving entry prices beyond what reacting to the book can achieve.
