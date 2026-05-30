# P1 variant — feed-driven resolution-drift backtest (Option-2)

Trigger driven by a **real station feed (IEM ASOS)** instead of the Open-Meteo model. Unit-aware bucketing (°F ranges for US, °C single-degree for London/Paris). **NYC = in-sample; London/Paris/Chicago/Miami = OUT-OF-SAMPLE.**

## (3c) Decisive gate — real-feed bucket accuracy (hindsight)

Per resolved event: IEM daily-max over the event's local day → bucket (unit-aware) vs the resolved winner. Source = **iem**. Covered **281/281** resolved events.

- **Overall accuracy: 99.6%** (gate to clear: > 70% → **PASS**)

| group | n | accuracy |
|---|--:|--:|
| region:US | 169 | 100.0% |
| region:intl | 112 | 99.1% |
| Chicago | 56 | 100.0% |
| London | 56 | 98.2% |
| Miami | 56 | 100.0% |
| New York City | 57 | 100.0% |
| Paris | 56 | 100.0% |

For reference, the Open-Meteo model proxy scored ~35% on the same task (reports/backtest_p1.md). A real station feed clearing the gate is the precondition for any feed-driven trade to have information value.

## (3d) Causal feed-driven backtest

Causal lock (no peek at the final high): once local clock ≥ lock-hour AND temp has fallen `decline_margin_f` below the running max for `decline_readings` readings, the running-max bucket is the feed's pick. Enter that bucket's YES at the first print at/after lock (trade-stream proxy — optimistic: no queue/slippage/impact); hold to resolution. Sweep the lock hour.

### OUT-OF-SAMPLE (London + Paris + Chicago + Miami) — the real test

| lock hr | n | feed-hit | avg entry | edge/share | ROI(fee0) | ROI(fee 2%) |
|--:|--:|--:|--:|--:|--:|--:|
| 12 | 196 | 0.84 | 0.780 | +0.0569 | 0.110 | 0.081 |
| 13 | 197 | 0.89 | 0.835 | +0.0587 | 0.063 | 0.037 |
| 14 | 197 | 0.92 | 0.890 | +0.0335 | 0.020 | -0.002 |
| 15 | 197 | 0.95 | 0.925 | +0.0292 | 0.006 | -0.015 |
| 16 | 197 | 0.97 | 0.957 | +0.0129 | 0.012 | -0.009 |
| 17 | 197 | 0.98 | 0.974 | +0.0106 | 0.011 | -0.009 |

### In-sample (NYC) — for comparison

| lock hr | n | feed-hit | avg entry | edge/share | ROI(fee0) | ROI(fee 2%) |
|--:|--:|--:|--:|--:|--:|--:|
| 12 | 54 | 0.96 | 0.939 | +0.0236 | 0.022 | 0.001 |
| 13 | 54 | 0.98 | 0.954 | +0.0272 | 0.031 | 0.010 |
| 14 | 54 | 0.98 | 0.963 | +0.0183 | 0.023 | 0.003 |
| 15 | 54 | 0.98 | 0.975 | +0.0067 | 0.020 | -0.000 |
| 16 | 54 | 0.98 | 0.980 | +0.0020 | 0.010 | -0.011 |
| 17 | 54 | 1.00 | 0.988 | +0.0120 | 0.009 | -0.011 |

### Per-city @ lock hour 13

| city | n | feed-hit | avg entry | ROI(fee0) | ROI(fee 2%) |
|---|--:|--:|--:|--:|--:|
| Chicago | 31 | 0.97 | 0.872 | 0.061 | 0.040 |
| London | 55 | 0.82 | 0.770 | 0.101 | 0.074 |
| Miami | 56 | 1.00 | 0.938 | 0.053 | 0.032 |
| New York City | 54 | 0.98 | 0.954 | 0.031 | 0.010 |
| Paris | 55 | 0.82 | 0.774 | 0.008 | -0.025 |

## Verdict

- Out-of-sample @ lock 13: feed-hit **0.89**, avg entry **0.835**, ROI **0.063** (fee0) / **0.037** (fee 2%), n=197.
- avg_entry < feed_hit out-of-sample at *every* lock hour? **True** — the market does NOT fully price the feed; a residual gap persists.
- Post-fee OOS ROI positive at lock hours: **[12, 13]** of [12, 13, 14, 15, 16, 17] (later locks → market already converged → entry ≈ hit → edge gone).
- Post-fee OOS positive in cities @ lock 13: **['Chicago', 'London', 'Miami']** of ['Chicago', 'London', 'Miami', 'Paris'] (dispersion = not structural).

- **NO robust, deployable edge.** A residual gap *is* present (avg_entry < hit everywhere), and post-fee OOS ROI is positive at the *earliest, most aggressive* lock hours — but it is NOT robust: it erodes to ~0 / negative as the lock hour moves into the afternoon, and it is negative for at least one out-of-sample city at the focus hour. The earliest-lock 'edge' is also where the optimistic trade-stream proxy is least trustworthy — you enter before the high is confirmed, competing with the fast snipers documented in exploits.md for the same convergence. **As-backtested this is not deployable; at best it is a candidate for forward paper validation** to test whether those early-lock entry prices are actually attainable against live competition (not assumed by the proxy).

## Caveats
- Trade-stream proxy (no historical order book): assumes the print at/after lock is takeable with no queue/slippage and ignores our own market impact — **optimistic**.
- IEM obs are ~hourly (US) / ~half-hourly (EU); a brief peak between obs can be missed, and the resolver may use a 6-hour max group we don't see → some gate misses are feed-granularity, not strategy, artefacts.
- Hold-to-resolution PnL; per-entry capacity capped at $50. ~60-day window per city.
