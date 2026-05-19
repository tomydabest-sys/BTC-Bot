# BTC-Bot — Polymarket BTC Up/Down Trading Bot

Automated trading bot for Polymarket's BTC Up/Down prediction markets, with Kelly-aware sizing, multi-strategy aggregation, decision-log diagnostics, and a live web dashboard.

> **⚠️ Live mode is currently disabled by a hard guard.** Live order placement requires EIP-712 signing which has not yet been implemented. Use paper mode (`mode: paper`) for development and backtesting. See [`DESIGN.md`](DESIGN.md) for the architectural overview.

---

## Quick Start

### 1. Install

```bash
git clone <repo-url>
cd BTC-Bot
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### 2. Configure

```bash
cp config.example.yaml config.yaml
cp .env.example .env
# Edit .env: at minimum, you need a POLYMARKET_API_KEY (read-only, free).
# For live mode (NOT YET SUPPORTED): also set POLYMARKET_PRIVATE_KEY.
```

### 3. Run paper-mode with dashboard

```bash
python -m polybot.dashboard.launcher --config config.yaml --mode paper
# Dashboard opens at http://localhost:8080
```

### 4. Phase-1 force-trade run (for diagnostics)

When you need to verify the full pipeline end-to-end without waiting for natural strategy triggers, use force-trade mode with the aggressive config:

```bash
# Linux / macOS
BOT_FORCE_TRADE=1 python -m polybot.dashboard.launcher --config config.aggressive.yaml --mode paper

# Windows PowerShell
$env:BOT_FORCE_TRADE='1'
python -m polybot.dashboard.launcher --config config.aggressive.yaml --mode paper

# Or use the bundled helper
.\run_phase1.ps1
```

Once you have ~30-50 trades in `data/bot.db`, switch to the calibrated config:

```bash
.\run_phase2.ps1
```

---

## Architecture overview

```
Polymarket Gamma API ── Market Scanner ──┐
Polymarket CLOB WS  ── Data Pipeline ────┤
Binance WS         ── Exchange Feed ─────┤
                                          ├─→ Strategies ─→ Aggregator ─→ Risk ─→ Execution
                                          │
                                          └── Health Monitor + Decision Log
```

- **Market Scanner**: discovers BTC up/down markets across 5m, 15m, 1h, 4h, daily timeframes
- **Strategies**: `overshoot_reversion`, `boundary_decay`, `dual_direction_arb`, `maker_edge` (and several others — see `src/polybot/strategies/`)
- **Risk Manager**: Kelly-aware sizing with per-timeframe caps and hard floors
- **Execution Engine**: paper-mode simulated fills, live-mode hard-guarded
- **Decision Log**: every cycle writes one JSONL line per (strategy, market) — see `logs/decisions.jsonl` and `python -m polybot.analyze --since 5m`

Full design document: [`DESIGN.md`](DESIGN.md).

---

## Useful commands

```bash
# Tail decision log (block reasons, edge, confidence per cycle)
python -m polybot.analyze --since 5m

# Run tests
pytest tests/ -v

# Lint
ruff check src/

# Dry-run validate config without starting the bot
python -m polybot.dashboard.launcher --config config.yaml --no-bot
```

---

## Key configuration

See `config.example.yaml` for a fully commented, recommended starting point. The two notable shipping configs are:

- **`config.yaml`** — calibrated, conservative settings for normal operation
- **`config.aggressive.yaml`** — relaxed gates for Phase-1 force-trade diagnostics

Risk envelope defaults:

| Setting                      | Default | What it does                                    |
|------------------------------|---------|-------------------------------------------------|
| `bankroll_usd`               | 500     | Total simulated capital for paper mode          |
| `kelly_fraction`             | 0.50    | Half-Kelly (multiplied with confidence)         |
| `hard_cap_pct`               | 0.10    | No single trade exceeds 10% of bankroll         |
| `max_daily_loss`             | 25      | Circuit-break the day at $25 down               |
| `min_usd`                    | 2.0     | Minimum trade size; floors recovered Kelly=0    |

---

## Paper Validation Run

The bot ships a 30-day paper validation gate (see
`src/polybot/monitoring/paper_validation.py`) that is the gating
mechanism for any decision to flip the bot live. The gate evaluates
nine pass/fail metrics and only returns `READY` when every one of them
clears.

### 1. Start the run

```bash
python -m polybot.dashboard.launcher \
    --config config.maker_paper.yaml --mode paper --mock-btc-feed
```

`config.maker_paper.yaml` ships with `maker.enabled: true` and an
empty `strategies.enabled: []`, so the V2 maker stack (MakerOrchestrator
+ QuoteManager + InventoryManager + PaperValidationGate) is the
only income path under test. The shared risk envelope (bankroll
$500, hard cap 10%, daily loss cap $25, ATH drawdown kill at 40%)
mirrors `config.yaml` so the validation result is comparable.

State persists to `data/paper_validation.json`. The gate's 30-day
clock starts on the first run and survives restarts; it only resets
on an explicit call to `PaperValidationGate.reset()`. The gate
auto-saves every 5 minutes from the running bot.

### 2. Check progress

```bash
python scripts/validation_status.py
```

Sample output:

```
========================================================================
  BTC-BOT 30-DAY PAPER VALIDATION GATE
========================================================================
  Status:       NOT_READY
  Started:      2026-05-19T...
  Duration:     6.42 days
  Net P&L:      $+12.10
------------------------------------------------------------------------
  Metric                       Pass  Current        Threshold      ETA
------------------------------------------------------------------------
  duration_days                FAIL  6.42d          >= 30d         ~23.6d more
  trades                       FAIL  108            >= 500         392 more trades (~23.3d at current pace)
  net_pnl_usd                  OK    $+12.10        >= $0.00       —
  ...
```

Exit codes: `0` = `READY`, `1` = `NOT_READY`, `2` = `INSUFFICIENT_DATA`.
Use `--json` for a machine-readable form.

### 3. The nine gate metrics

| Metric                  | Threshold        | Why it gates the live flip |
|-------------------------|------------------|----------------------------|
| `duration_days`         | ≥ 30 days        | Smooths out single-week regime artefacts. |
| `trades`                | ≥ 500 round-trips| Statistical significance — at <500 round-trips the observed edge is dominated by sample noise. |
| `net_pnl_usd`           | > $0             | After-fee profitability — the bare minimum for a strategy to be worth running. |
| `sharpe_daily`          | ≥ 1.5            | Risk-adjusted return must be acceptable; rules out "lucky once" runs. |
| `max_drawdown_pct`      | < 15%            | Limits how brutal the worst observed loss path was. |
| `quote_uptime_pct`      | > 80%            | The maker stack must actually be quoting — uptime is the prerequisite for everything else. |
| `p95_latency_ms`        | < 150 ms         | Cancel/replace latency tail directly drives adverse selection; >150 ms p95 is unsafe in live. |
| `unhandled_exceptions`  | == 0             | Any crashed loop or untrapped exception during paper is a guaranteed crash in live. |
| `fee_consistency_pct`   | == 100 %         | Every order must have priced against a freshly-fetched fee rate. Hardcoded fees on even one trade contaminate the P&L attribution. |

### 4. Going live (do NOT do this until READY)

`LIVE_TRADING_ENABLED` in `src/polybot/data/client.py` is the final
flag. It MUST remain `False` until `scripts/validation_status.py`
reports `status: READY`. Flipping it earlier wires the live
order-placement code path and the bot will start signing real EIP-712
orders against real USDC.e on Polygon.

The recommended pre-flip checklist is:

1. `python scripts/validation_status.py` returns exit code 0.
2. Review the failing-metric tail of the last 7 days — `READY`
   should not be a freshly-flipped boolean.
3. Inspect `data/paper_validation.json` directly and confirm
   `unhandled_exceptions == 0` over the full run.
4. Only then: edit `LIVE_TRADING_ENABLED` and install the
   `[live]` extras (`pip install -e ".[live]"`).

---

## Live mode (NOT YET ENABLED)

Live trading requires:

1. EIP-712 typed-data signing of orders (via `py-clob-client` or hand-rolled web3)
2. USDC.e balance check on Polygon
3. Tested cancel-order flow

When this is implemented, live mode will be unlocked via `mode: live` in `config.yaml`. Until then, the bot raises `NotImplementedError` at startup if `mode: live` is set.

---

## Project layout

```
BTC-Bot/
├── README.md                 # this file
├── DESIGN.md                 # architecture deep-dive
├── config.yaml               # default calibrated config
├── config.aggressive.yaml    # Phase-1 force-trade config
├── config.example.yaml       # commented starter
├── .env.example              # secrets template
│
├── src/polybot/              # package
│   ├── main.py               # Bot orchestrator + trading loop
│   ├── config.py             # Pydantic-validated config
│   ├── data/                 # Polymarket client, WS, exchange feed, models
│   ├── strategies/           # signal generators
│   ├── risk/                 # sizing + circuit breaker
│   ├── execution/            # order placement / lifecycle
│   ├── positions/            # P&L tracking + exit logic
│   ├── scanner/              # market discovery
│   ├── monitoring/           # alert channels
│   ├── dashboard/            # FastAPI dashboard + frontend
│   └── diagnostics/          # decision_log + block-reason taxonomy
│
├── tests/                    # pytest suite
├── scripts/                  # one-off utilities (synthetic_load, etc)
└── docker/                   # containerization
```

---

## License & disclaimers

This is research / personal-use software. It is **not** investment advice. Prediction markets are speculative. Use at your own risk. Authors and contributors disclaim liability for any losses incurred from running this bot.
