# P1 resolution-drift backtest (prototype)

**Method (honest):** entries triggered by a *causal* peak-passed rule on the hourly KLGA reference (no peek at the final daily max); executable price = first YES print at/after the signal (trade-stream proxy; no order-book depth). Hold to resolution. Single-city/30-day window — indicative, not out-of-sample.

## Strategy (with peak gate)

- Fired on **8** of 297 resolved markets | hit rate **0.25** | avg entry **0.3182** | avg edge/share **-0.0683**
- Deployed **$61** | PnL(fee 0) **$-10** (ROI -0.157) | PnL(fee 0.02) **$-19** (ROI -0.3069)

## Baseline (NO peak gate — enter as soon as running-max enters the bucket)

- Fired on **42** | hit rate **0.2381** | avg edge/share **0.0247** | PnL(fee 0) **$-12** (ROI -0.0481)

The peak gate should raise hit rate / edge vs the baseline — that delta is the value of waiting for the causal peak signal (the baseline buys winners AND buckets the temperature later climbs out of).

## ⚠️ Diagnosis — why this fails: the reference cannot pick the 2°F bucket
- Open-Meteo daily-max lands in the **actual winning bucket only 12%** of 17 events.
- It carries a **systematic bias of 1.36°F** (median |bias| 1.6°F) vs the resolved bucket centre — and the buckets are only **2°F wide**. The modeled 2m temperature reads systematically *hotter* than the official station high used to resolve, so the rule keeps buying the bucket one step too high.
- **Conclusion:** the ex-post resolution-drift edge is real (exploits.md: ~$0.146/share on $1.32M of winning-side flow) but **NOT capturable with Open-Meteo** as the reference. The backtest correctly kills the naive implementation.

## Fix direction (validated spot-check)
- Use the **actual station observation feed** (NWS METAR for KLGA via `api.weather.gov`, or the Wunderground resolver) instead of Open-Meteo. Spot-check: NWS KLGA daily max **84.2°F → resolved bucket 84-85°F (exact match)** on 2026-05-27, vs Open-Meteo's bias. (NWS API retention is only ~3 days, so this validates the *live* path; deep historical NWS backtests aren't possible here.)
- Or **bias-correct** Open-Meteo (subtract the measured ~mean bias) and **avoid entries when the reading is within ~1-2°F of a bucket edge**.
- Or make the **market price itself** the trigger (trade convergence once the book has already singled out a winner), rather than the weather model.

```
{'min_peak_hour_local': 13, 'decline_margin_f': 1.0, 'taker_fee': 0.0, 'taker_fee_stress': 0.02, 'per_entry_cap_usdc': 50}
```

## Caveats
- Trade-stream proxy is optimistic: assumes we get the next print's price with no queue/slippage and ignores that our own flow would move it.
- Hourly reference ⇒ ±1h peak timing error, the main driver of mis-fires.
- In-sample single city; validate on held-out cities (scale-up) before sizing.
- Fee units for Polymarket weather markets unconfirmed; shown at 0 and a stress level.
