# Phase 7 — BTC-Bot recommendations (PROPOSALS ONLY)

> Read-only analysis. **Nothing here is implemented in BTC-Bot.** Each proposal lists evidence, expected effect, an implementation sketch, and risk/assumptions. Machine-readable calibration is in `reports/insights.json`.

## Calibration snapshot (from the observed bot population)
- Bots (score≥0.5): **191**, strong (≥0.7): **17**
- Median bot: **20.0/24** active hours, breadth **48.0** markets, median size **6.065** shares, up to **4.0** fills/s, win rate **0.7576**, ROI **0.005**.

## P1 — Resolution-drift capture (highest priority)
- **Evidence:** dossiers + exploits.md H1 — dedicated snipers harvest the convergence of the winning bucket's YES to $1 on the resolution day (op_004: 99% resolution-day trades, win 0.91, thin ROI).
- **Proposal:** add a resolution-drift strategy that, once the reference running-max temperature has entered a bucket and the day is past its peak, buys the residual gap to $1 (and sells exceeded buckets toward $0).
- **Expected effect:** high-hit-rate, thin-edge flow — many small wins, the dominant profitable pattern observed.
- **Implementation sketch:** consume `insights.json` + a station temperature feed; gate entries on (running_max in-bucket) AND (local time > climatological peak hour); size small; hold to resolution.
- **Risk/assumptions:** mis-timing before the daily peak flips winners to losers; reference latency vs the existing fast snipers; capacity is bounded.

## P2 — Fee/rebate-aware, fixed-size maker quoting
- **Evidence:** top bots are high-breadth, fixed-ish small sizes, ~0.88-0.94 win rate, thin positive ROI ⇒ spread/fair-value capture, not directional bets.
- **Proposal:** maker-quoting with small fixed notional per bucket and rebate-aware placement (Polymarket rewards/maker fee schedule from Gamma `makerBaseFee`/rewards tags).
- **Implementation sketch:** quote size ≈ observed median (~6.065 shares); breadth across buckets; tick-offset from fair value at the 0.001 tick.
- **Risk/assumptions:** adverse selection (unmeasured here — needs Phase 8 live capture); rebate economics can flip with fee changes.

## P3 — Latency / cadence target
- **Evidence:** fastest bots reach multiple fills/second and are 22.0/24 active.
- **Proposal:** set a polling/reaction budget of **sub-second on the order path and sub-minute on the reference** to compete for P1; do not chase pure latency arb (observed to be largely unprofitable post dynamic fees).
- **Risk/assumptions:** infra cost; diminishing returns past 'good enough'.

## P4 — Market-selection filter
- **Evidence:** 48.0-market breadth among bots, but exploits.md H3 shows many <20-trade tail buckets; H2 shows thin off-peak hours.
- **Proposal:** prioritise liquid central buckets on the resolution day; treat tail buckets only as small resolution-short candidates; avoid quoting in the thinnest UTC hours unless pick-off spreads justify it.
- **Risk/assumptions:** concentration risk; thin-hour edges may not exist until verified on live-book data.

## P5 — Sizing model
- **Evidence:** bot sizes are small and often fixed (mode-size fractions high for ladder/grid archetypes); ROI is thin and win-rate-driven.
- **Proposal:** small fixed/tiered sizing per bucket rather than aggressive Kelly on long-shot edges (echoes BTC-Bot's prior tail-price blow-up bug); cap per-bucket and per-event exposure.
- **Risk/assumptions:** under-sizing leaves edge on the table; calibrate to bankroll.

## Insight artifact
`reports/insights.json` carries the calibrated numbers (active hours, sizes, breadth, win rates, ROI bands, constraints) for BTC-Bot to consume programmatically.

## Which proposals to take forward?
Please tell me which of **P1–P5** to develop into a detailed spec (still as a proposal — no BTC-Bot code changes without your go-ahead), and whether to widen the allowlist / scale the sample first to harden the evidence.
