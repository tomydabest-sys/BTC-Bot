# P1 variant — market-price-triggered resolution-drift backtest

No weather feed. Trigger: a bucket's YES price crosses **θ** on the resolution day (resolution-day only); enter at that YES print (trade-stream proxy), hold to resolution. Causal — outcome used only for PnL. Single-city/30-day window — indicative.

| θ | entries | hit rate | avg entry | avg edge/share | deployed$ | PnL(fee0) | ROI(fee0) | ROI(fee 0.02) |
|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 0.5 | 52 | 0.52 | 0.535 | -0.0158 | 2,556 | -43 | -0.017 | -0.055 |
| 0.6 | 44 | 0.61 | 0.633 | -0.0192 | 2,128 | -4 | -0.002 | -0.034 |
| 0.7 | 35 | 0.77 | 0.741 | +0.0302 | 1,691 | 126 | 0.075 | 0.048 |
| 0.8 | 30 | 0.90 | 0.828 | +0.0720 | 1,452 | 179 | 0.124 | 0.099 |
| 0.9 | 28 | 0.96 | 0.910 | +0.0547 | 1,357 | 127 | 0.093 | 0.071 |
| 0.95 | 26 | 1.00 | 0.955 | +0.0447 | 1,300 | 61 | 0.047 | 0.026 |

## Read
- **Higher θ → higher hit rate but higher entry price** (thinner residual gap to $1). The question is whether any θ stays **net positive after fees**.
- Best θ by ROI(fee0) gives ROI **0.1236** (hit 0.9, entry 0.828) — **POSITIVE** at zero fee; under the stress fee 0.02 ROI is 0.0994.
- If even the best θ is ~0/negative after fees, reacting to the book is **too late** — the convergence is already priced. Then P1 only works with a signal *faster than the market* (the resolution-drift snipers' actual edge), not by following price.

## Caveats
- Trade-stream proxy (no historical book): assumes the print price is takeable with no queue/slippage and ignores our own market impact — **optimistic**.
- Single city / 30 days, in-sample; validate out-of-sample before sizing.
- Capacity = YES notional transacting at/after the crossing (per-entry cap $50).
