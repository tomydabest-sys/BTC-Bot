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
