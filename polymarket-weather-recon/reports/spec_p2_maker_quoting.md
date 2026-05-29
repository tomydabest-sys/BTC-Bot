# Spec — P2: Fee/rebate-aware fixed-size maker quoting (PROPOSAL ONLY)

> Read-only design doc for BTC-Bot review. **Nothing here is implemented.**
> Evidence from the bounded recon window (taker-side Data API) — so the maker
> side is **inferred**, not directly observed. The honest caveat (no historical
> maker quote/cancel data) drives the validation plan below.

## 1. Thesis & evidence
The persistently-profitable wallets are **high-breadth, tiny-size, high-win-rate,
thin-ROI** — the signature of spread / fair-value capture (market making), not
directional betting.

Measured (`insights.json`, bot population ≥0.5):
- median size **~6 shares**, breadth **48 markets** (strong bots **154**),
  up to **4 fills/s** (strong bots more), median fill **win rate 0.76**, median
  **ROI 0.005** (≈50 bps on resolved notional).
- Strong bots are **20–22h/24** active across **many** buckets simultaneously.

Interpretation: earn the spread + Polymarket maker **rewards/rebates** by quoting
small size on both sides of many buckets near a computed fair value, churning
quotes as the fair value moves. We **cannot** see their quote/cancel ladder
(off-chain) — so we propose the behaviour and *measure it forward*.

## 2. Fair value & market mechanics
- Tick `orderPriceMinTickSize = 0.001`; `orderMinSize = 5`; neg-risk multi-bucket
  events (~11 buckets); `makerBaseFee` observed `1000` (units unconfirmed — pull
  the live per-token fee via CLOB `/fee-rate` before trusting fee math) and
  reward eligibility flagged by Gamma `Rewards Automation …` tags.
- **Fair value** per bucket = model probability the daily high lands in that
  bucket, from BTC-Bot's `data/forecasts/ensemble_blender.py` (it already handles
  point + range markets, °C/°F). Neg-risk buckets should be normalised so YES
  probabilities across the event sum to ~1.

## 3. Quoting logic
```
for event in active_weather_events:
    fv = ensemble_blender.bucket_probabilities(event)     # sums ~1 across buckets
    for bucket in event.buckets:
        p = fv[bucket]
        if p < p_floor or p > p_ceiling: continue          # skip ~dead/certain buckets
        half = max(min_half_spread, k_vol * sigma(p))       # spread from model uncertainty
        bid = round_to_tick(p - half - rebate_adj)
        ask = round_to_tick(p + half + rebate_adj)
        post maker BID @ bid size=q, maker ASK @ ask size=q  # q small, fixed
        cancel/replace if |mid_model_move| > reprice_threshold
risk: cap inventory per bucket; skew quotes against current inventory.
```
- **Rebate-aware:** widen/keep quotes inside the reward band when a bucket is
  reward-eligible (rewards can dominate the thin spread). `rebate_adj` nudges
  quotes to stay reward-qualifying while limiting adverse fills.
- **Inventory skew:** as net position in a bucket grows, skew quotes to flatten
  (standard MM); hard inventory cap per bucket/event.

## 4. Parameters (proposed defaults)
| param | default | rationale |
|---|---|---|
| `q` (quote size) | 5–10 shares | observed tiny median (~6); also `orderMinSize=5` |
| `min_half_spread` | 0.01 (10 ticks) | cover fees+noise on a 0.001 tick |
| `k_vol` | tune | spread scales with model uncertainty |
| `p_floor / p_ceiling` | 0.05 / 0.95 | don't quote ~dead/certain buckets |
| `reprice_threshold` | 0.005 | churn budget vs the fastest bots (4+ fills/s) |
| `inventory_cap_bucket` | \$15 | from weather_risk |
| reward band | from Gamma rewards tag | keep quotes reward-qualifying |

## 5. Integration sketch (described, not implemented)
- Extends BTC-Bot's existing **`strategies/maker_rebate.py`** stub.
- Fair value from `data/forecasts/ensemble_blender.py`; placement/cancel via
  `exchanges/clob_client.py` (and the V2 client / `ws_orderbook.py` skeleton for
  fast book updates); fee via CLOB `/fee-rate?token_id=`.
- Risk: per-strategy exposure cap (handoff Bug-D), inventory caps in
  `risk/weather_risk.py`; respect EIP-712 domain "2" and batch size 15 noted in
  the handoff.

## 6. Validation plan — Phase 8 first (critical)
Because historical maker behaviour is unrecoverable, **gather forward data before
committing**:
1. Run the recon project's **Phase 8 `clob_live_capture.py`** to snapshot
   top-of-book + depth for active weather buckets at a fixed cadence.
2. From that, measure **realised spread, fill rate, adverse selection** (mid move
   at horizon h after a maker fill) and **reward accrual** per bucket/hour.
3. Backtest the quoting rule against captured books; only then paper-trade
   through BTC-Bot's validation gate (≥14 days) before real money.
- **Success metric:** positive net of (spread captured + rebates − adverse
  selection − fees), with bounded inventory and no reliance on a single bucket.

## 7. Risks & assumptions / failure modes
- **Adverse selection is the whole game and is currently unmeasured** — a maker
  that is picked off after temperature moves loses; P1's reference signal is
  exactly what would pick *us* off. Quote defensively near `peak_passed`.
- **Reward-rule dependence:** economics can flip if Polymarket changes the reward
  schedule; do not assume current rebates persist.
- **Latency/cancel race:** if repricing is slower than the fast bots, we eat the
  stale-quote losses we identified in `exploits.md` H4.
- **Maker-side inference risk:** the whole thesis rests on taker-side inference;
  the forward capture in step 6 is what converts inference into evidence.

## 8. Sequencing recommendation
Do **P1 first** (testable on existing/replayable data, clearer edge), and gate
**P2 behind Phase 8 live-book capture** so its core risk (adverse selection) is
measured rather than assumed.
