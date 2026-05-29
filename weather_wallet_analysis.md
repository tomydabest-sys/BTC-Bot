# Weather-wallet analysis (regenerated 2026-05-29)

This file is referenced by the handoff brief as the source of "the 4 profitable
wallets" for the wallet-watch confirmation signal. **It was not in the repo** —
so this is a fresh, honest reconstruction from live Polymarket data, not a copy
of the original. Read the caveats before trusting any address.

## What was actually in the repo

- `wallet_profiler.py` ships **11 default addresses**. Pulling their live
  `data-api.polymarket.com/activity` shows they are **BTC/ETH/SOL "Up or Down"
  5-minute scalpers**, not weather traders. None had any weather-market
  activity. They are leftovers from the project's BTC-bot origins and must
  **not** be used as weather confirmation wallets.

## How candidates were rediscovered

`scripts/polyweather/discover_weather_wallets.py`:

1. Pull active weather events from Gamma (strict temperature filter, the same
   one the scanner uses).
2. For each event's markets, list the wallets trading them
   (`data-api /trades?market=<conditionId>`). A 15–40 market sweep finds
   **800–900 unique weather traders**, so liquidity is real.
3. For the most-active traders, sum their weather-position PnL from
   `/positions`: `realizedPnl` (closed portions) + `cashPnl` (open) as a
   ranking **proxy**.

## The honest result

A snapshot proxy **does not cleanly identify profitable weather wallets.** In
repeated sweeps the highest-frequency weather traders mostly showed **negative**
`cashPnl` (large open positions marked at a loss mid-resolution), and the
`realizedPnl`/`cashPnl` split is noisy because most positions are still open.

Example sweep (15 events, top 10 by weather-trade count):

| wallet (trunc) | wTrades | wxPos | realPnL | cashPnL | proxy |
|---|--:|--:|--:|--:|--:|
| 0xaa2873…090b | 51 | 500 | 639 | -1262 | -623 |
| 0xbb7a6e…ed18 | 46 | 500 | 660 | -1287 | -627 |
| 0x173516…0319 | 66 | 493 | 654 | -1422 | -767 |
| 0x16d45f…a868 | 37 | 500 | -1808 | -7976 | -9784 |

A separate sweep (40 events) surfaced a few wallets with large **open** weather
books and mildly positive snapshot PnL — listed as commented `candidate_wallets`
in `config/polyweather/wallets.yaml`. They are **unverified**: large open
exposure is not proven profit.

## Recommendation

- `config/polyweather/wallets.yaml` ships with `watch_wallets: []` on purpose.
- Wallet-watch is wired as a **dashboard-only confirmation signal** and never
  gates or sizes trades. It is safe to leave empty.
- To populate it credibly, track a candidate's **closed round-trip** weather
  P&L over days (snapshot proxy is not enough), or supply the original four
  addresses if they can be recovered. Then add `{address, label}` entries to
  `watch_wallets`.

## Operator decision needed

If you have the original four addresses, paste them into `watch_wallets`.
Otherwise the safe default is to run with the signal empty until a candidate
proves itself on closed round-trips. We are **not** hardcoding unverified
addresses as "profitable."
