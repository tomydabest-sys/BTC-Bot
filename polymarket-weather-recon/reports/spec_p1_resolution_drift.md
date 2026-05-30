# Spec — P1: Resolution-drift capture (PROPOSAL ONLY)

> Read-only design doc for BTC-Bot review. **Nothing here is implemented.**
> Evidence is from the bounded recon window (NYC temperature, 30 days,
> taker-side Data API). Revalidate out-of-sample before sizing up.

## 1. Thesis & evidence
Daily-temperature buckets resolve on the day's high. As the day's running-max
temperature settles, the **winning** bucket's YES must converge to \$1 and every
other bucket's YES to \$0. That convergence is not instant — it is bought up by
dedicated snipers.

Measured in this window:
- **42,825** winning-outcome BUY fills occurred **on the resolution day** at an
  **average price of 0.854** ⇒ **~\$0.146/share of convergence was still
  available**, across **~\$1.32M** of winning-side notional.
- **72.7%** of all volume trades on the resolution day.
- Dossier `op_004` (3-wallet operator) is a textbook sniper: **99%** of trades on
  the resolution day, fill win rate **0.91**, razor-thin ROI (many small wins).

The edge is real but thin and hit-rate-driven; the competition is fast
(multi-fill-per-second). This is an *execution/timing* edge, not a forecasting
edge.

## 1a. ⚠️ Backtest result (Option-1 prototype) — REQUIRED design change
A causal prototype (`reports/backtest_p1.md`) showed the naive
reference-driven version is **net negative** (hit rate 25%, ROI −16%). Root
cause, measured: even with correct whole-degree rounding, **Open-Meteo's daily max
lands in the actual winning bucket only ~35% of the time** (and the causal,
trade-before-the-final-peak version does worse, ~25%), because of a **systematic
+1.36°F warm bias** (median |bias| 1.6°F) vs the resolved bucket — and the buckets
are only **2°F wide**. The modeled 2m temperature reads hotter than the official
station high used to resolve, so the rule buys the bucket one step too high.

**Therefore a weather-model reference (Open-Meteo) cannot drive bucket-level
entries.** Two viable paths remain: (a) the actual station feed (NWS METAR /
Wunderground), or — preferred and now backtested — (b) the **market price's own
convergence** as the trigger (no weather feed at all).

## 1b. ❌ Market-price trigger: looked good in-sample, FAILED out-of-sample
The market-price trigger (enter a bucket's YES once its price crosses θ on the
resolution day, hold to resolution) was **net positive on NYC alone** but **does
NOT generalise** when validated on other cities (`reports/backtest_p1_market.md`):

| group | θ=0.8 hit | θ=0.8 ROI (fee0) | θ=0.8 ROI (2% fee) |
|---|--:|--:|--:|
| NYC (in-sample, 60d) | 0.86 | **+6.6%** | +4.2% |
| **Out-of-sample (LON+PAR+CHI+MIA)** | 0.80 | **−3.3%** | **−5.6%** |

Per-city @ θ=0.8: NYC +6.6%, Miami +3.2%, Chicago −2.4%, London −6.6%, Paris −6.5%.
And NYC itself fell from +12.4% (30d, n=30) to **+6.6%** (60d, n=66) as the sample
grew. **Verdict: the apparent edge was an NYC-specific microstructure/liquidity
artifact, not a structural mispricing. Do NOT implement the price-triggered
version.** Reacting to the book is too late — by the time price reveals the winner,
the convergence is already (correctly) priced in most cities.

This leaves only path (a): a signal **faster and more accurate than the market** —
i.e. a real station feed (NWS METAR) that identifies the winning bucket *before*
the price converges. That is unproven (see §1a: needs a METAR archive to backtest
and high NWS-vs-resolver agreement to be confirmed) and should not be assumed to
work. **P1 is not currently a validated, implementable strategy.**

## 2. Market selection (preconditions)
- Weather **daily-temperature neg-risk events** (the ~11-bucket structure from
  Phase 1), one station per event (parsed from the Wunderground `resolutionSource`,
  e.g. KLGA). Reuse BTC-Bot's `data/stations/station_resolver.py`.
- Only trade an event on its **resolution day**, after a station temperature
  reading exists.
- Require minimum book liquidity on the target bucket (avoid the 9/319 <20-trade
  tail buckets unless running the H3 short variant).

## 3. Signal definition
Inputs: station temperature feed — **use `nws_client.py` (actual METAR), not
`open_meteo_client.py`**, per §1a — Gamma market state, CLOB book
(`exchanges/clob_client.py`).

Per event, maintain on the resolution day:
- `running_max` = max observed station temperature so far today (°F).
- `peak_passed` = local clock time is **after** the climatological/observed daily
  peak hour AND temperature has been flat/declining for `decline_confirm` readings
  (so `running_max` cannot realistically be overtaken).

Per bucket `b` with bounds `[lo_b, hi_b]`:
- **LONG-YES signal** (buy convergence) when `lo_b ≤ running_max ≤ hi_b` AND
  `peak_passed` AND `book_ask_b < 1 − min_edge`.
- **SHORT-YES / BUY-NO signal** when `running_max > hi_b` (bucket already exceeded,
  YES is dead) AND `book_bid_b > max_dead_price`.

`min_edge` is the convergence we insist on capturing (proposed 0.03–0.05, vs the
0.146 average left on the table — leaves margin for the existing snipers).

## 4. Entry / sizing / exit
```
on each cycle (resolution day, per event):
    temp = station_feed.latest(station)          # Open-Meteo hourly / NWS obs
    running_max = max(running_max, temp.value)
    if not peak_passed(event, now, running_max): return
    for bucket in event.buckets:
        lo, hi = bucket.bounds
        if lo <= running_max <= hi:               # winner
            ask = book.best_ask(bucket.yes_token)
            if ask is not None and ask <= 1 - min_edge:
                size = drift_size(bankroll, ask)   # small, see below
                propose BUY YES @ ask (IOC/limit), hold to resolution
        elif running_max > hi:                     # dead bucket
            bid = book.best_bid(bucket.yes_token)
            if bid is not None and bid >= max_dead_price:
                propose SELL YES @ bid (or BUY NO)
exit: hold to on-chain resolution; redeem winners.
```
- **Sizing** (`drift_size`): small, edge-scaled but **capped hard** —
  `size = clip(base_notional * (edge / target_edge), min, cap)`. Echoes BTC-Bot's
  prior tail-price blow-up bug: do **not** Kelly-max long-shot bets. Per-bucket
  and per-event caps from `risk/weather_risk.py`.
- **Exit:** hold to settlement; verify outcome from the on-chain CTF payout when
  an RPC is available, else Gamma resolution (acceptable for paper).

## 5. Parameters (proposed defaults)
| param | default | rationale |
|---|---|---|
| `min_edge` (winner) | 0.04 | capture convergence with margin under the 0.146 observed |
| `max_dead_price` (dead bucket) | 0.05 | only short clearly-dead buckets |
| `decline_confirm` | 2 readings | peak-passed confirmation |
| `base_notional` | \$2–\$5 | matches tiny observed bot sizes (median ~6 shares) |
| `per_bucket_cap` | \$10 | from weather_risk caps |
| `per_event_cap` | \$40 | concentration limit |
| reference poll | ≤60 s | sub-minute reference; order path sub-second |

## 6. Integration sketch (described, not implemented)
- New strategy module alongside `strategies/resolution_meanrev.py`, e.g.
  `strategies/resolution_drift.py`, returning at most one signal per bucket per
  event per cycle (heed the prior `negative_risk_arb` basket-spam bug — gate per
  event/bucket).
- Reuse `data/forecasts/open_meteo_client.py` for the station running-max;
  `station_resolver` for station mapping; `clob_client` for book.
- Risk via `weather_risk.py` caps + per-strategy exposure (the handoff's Bug-D
  fix). Consume calibration from `reports/insights.json`.

## 7. Validation plan (no live claims)
- **Backtest** on held-out cities/days: simulate fills at the recorded book
  ask/bid at signal time; PnL = Σ(1 − entry) on YES winners − losses from entries
  taken before a late temperature overtake. Report hit rate, ROI, max drawdown,
  capacity (filled notional vs available).
- **Paper** for ≥14 days against live markets through BTC-Bot's existing
  paper-fill path and validation gate before any real money.
- **Success metric:** positive ROI after fees at ≥0.85 hit rate and a capacity
  that justifies the infra.

## 8. Risks & assumptions / failure modes
- **Late overtake:** temperature rises again after `peak_passed` → winner flips →
  losses. Mitigate with conservative `peak_passed` (decline confirmation + clock).
- **Reference vs resolution mismatch:** Open-Meteo/NWS KLGA is a *proxy*;
  Wunderground (the actual resolver) can differ by 1°F near a boundary → boundary
  buckets are the riskiest. Avoid entries when `running_max` is within ~1°F of a
  bucket edge.
- **Crowded/fast:** existing snipers may leave little edge; thin capacity.
- **Fees** can erode the thin per-fill edge — model the maker/taker fee per token.
