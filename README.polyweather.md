# PolyWeather-Bot v1

Polymarket weather-market trading bot built on top of the BTC-Bot scaffolding. Trades temperature/precipitation buckets across NYC, Chicago, Dallas, Atlanta, Miami, LA, London, HK, Seoul, Paris.

> **Honest target.** A well-built £1k bot has a realistic envelope of **£30–£150/month median, £200–£500/month top decile, with 1-in-4 months flat or negative.** The £3–5k/month "stretch wish" is not realistic from a £1k bankroll in 30 days — public Polymarket leaderboard data shows the best weather bot running on a $649k open-position bankroll. Plan in 6–12-month horizons.

## Quickstart (mock mode — no creds needed)

```bash
git clone <repo> btc-bot && cd btc-bot
python -m venv .venv && source .venv/bin/activate     # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pip install scipy pyyaml

export BOT_MOCK_DATA=true                              # PowerShell: $env:BOT_MOCK_DATA="true"
python scripts/polyweather/paper_run.py --mock --duration 60
# Open http://127.0.0.1:8080 in a browser
```

Within 30 seconds the dashboard renders 6 populated tabs:

1. **Overview** — bankroll, P&L windows, equity curve, drawdown, heartbeat, mode banner.
2. **Per-City Edge** — heatmap of `edge × fill probability` for every city × bucket evaluated.
3. **Active Forecasts** — current decisions per market with model probability, edge, confidence.
4. **Risk** — kill-switch state, P&L by strategy, exposure caps.
5. **Trade Log** — last N closed trades.
6. **Validation Gate** — 9 paper-validation criteria; gate must be GREEN before live.

## Going live (gated)

```bash
python scripts/polyweather/discover_markets.py        # one-shot Gamma scan, audit station mappings
python scripts/polyweather/verify_station_mapping.py  # interactive: mark each market verified

# 14 days + 100 closed trades later
python scripts/polyweather/paper_run.py               # no --mock, real APIs

# Once /api/weather/validation-gate reports READY FOR LIVE: YES
BOT_MODE=live python scripts/polyweather/live_run.py --confirm-live
# Or override (NOT recommended) with strict $5 first-24h cap:
BOT_MODE=live python scripts/polyweather/live_run.py --confirm-live --force-live --bankroll-cap-usdc 200
```

## File layout

```
src/polybot/polyweather/
  exchanges/        polymarket_v2_client, gamma_client, data_api_client, ws_orderbook
  data/forecasts/   nws_client, open_meteo_client, met_office_client, ensemble_blender
  data/stations/    station_resolver + station_catalog.yaml
  data/climatology/ ncei_base_rates
  strategies/       weather_ensemble, negative_risk_arb, resolution_meanrev, maker_rebate
  risk/             validation_gate (9 criteria), weather_risk (caps + kill switches)
  persistence/      store (SQLite paper.sqlite / live.sqlite)
  dashboard/        routes + frontend (index.html, app.js, styles.css)
  orchestrator/     engine (the paper-loop core)

config/polyweather/
  risk.yaml         bankroll, caps, drawdown
  markets.yaml      target cities, station mapping seed
  strategy_weights.yaml  70/20/10 mix + edge thresholds

scripts/polyweather/
  paper_run.py / live_run.py / discover_markets.py
  verify_station_mapping.py / reset_paper_state.py / backtest.py

tests/polyweather/  full pytest module, plus fixtures under tests/fixtures/polyweather/
```

## What's in v1 (and what isn't)

In: mock-clean paper engine, 6-tab dashboard, 3 strategies + maker-rebate, ensemble blender (ECMWF/GFS/UKMO/GEFS), station resolver with audit log, 9-criterion validation gate, Decimal money math, $5 first-24h-live position cap (hard-coded), 5s heartbeat task, all 50 polyweather tests passing alongside the 205 existing BTC-Bot tests.

Out: full historical backtest replay, Discord/X/news ingestion, multi-broker, and any "lower-the-threshold-to-fire-more" tuning. Resist scope creep.

## Forbidden-list cross-check

- ✅ `from __future__ import annotations` is first after each module docstring.
- ✅ All money math uses `decimal.Decimal`. Probabilities use `float`.
- ✅ Fees fetched dynamically via `fetch_fee_rate_bps(token_id)` (mock returns 0 for maker, live calls SDK).
- ✅ EIP-712 domain version is the string `"2"` (constant in `polymarket_v2_client.py`).
- ✅ Polymarket V2 batch size 15.
- ✅ Heartbeat runs in its own `asyncio.Task` (test_polymarket_v2_client_mock asserts ≥3 ticks).
- ✅ Dashboard binds 127.0.0.1 by default; SSH-tunnel to a VPS, do not expose port 8080.
- ✅ Separate paper.sqlite vs live.sqlite (forbidden-list rule #10).
- ✅ Decimal serialised via custom JSON encoder (no `TypeError` mid-response).
- ✅ Empty-state messaging on every dashboard tab (no blank panels).
