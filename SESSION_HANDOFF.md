# BTC-Bot v4 Rewrite — Session Handoff

Repo: `tomydabest-sys/BTC-Bot` · default branch:
`claude/polymarket-bot-design-VFErr` · working branch: `v4-rewrite`
(everything below has already been merged into default).

## Run it

```bash
pip install -e ".[dev]"
python -m polybot.dashboard.launcher --config config.yaml --mode paper
```

Dashboard at <http://localhost:8080>. Optional: `--port`, `--no-bot`,
`--no-dashboard`, `--mock-btc-feed`.

**No API or private keys required for paper mode.** A
`POLYMARKET_API_KEY` is optional (suppresses a startup warning, slightly
relaxes Gamma rate limits). `POLYMARKET_PRIVATE_KEY` is only checked on
the live path, which is hard-guarded shut anyway.

## Test surface

`pytest tests/ -v` → **59 passed** (last verified 2026-05-09).

| File | What it covers |
| --- | --- |
| `tests/test_execution.py` | Paper / live engine, dual-leg, hard guard |
| `tests/test_positions.py` | Position lifecycle, partial fills, exits |
| `tests/test_risk.py` | Kelly sizing, can_open_position, can_place_order |
| `tests/test_strategies.py` | maker_edge inventory, overshoot_reversion, boundary_decay |
| `tests/test_pipeline.py` | Per-token orderbook isolation (regression for cross-token exit) |
| `tests/test_pricing_safety.py` | maker_edge target_price clamps, risk-gate price range, storage clear |

## What got done in this session (PRs #3 → #7, all merged)

A new ZIP rewrite was unzipped over the repo (PR #3). It introduced a
ton of integration drift; the rest of the PRs reconciled it and shook
out real bugs.

### PR #3 — initial v4 rewrite drop + integration fixes

Replaced strategies (`overshoot_reversion`, `boundary_decay`,
`maker_edge`), removed `dual_detection_arb.py` (typo'd duplicate, never
registered), added `tests/test_execution.py` and
`tests/test_positions.py`, live-mode hard guard, retention loop.

Reconciled the new `main.py` with surrounding components:

- `polybot.aggregator` import → `polybot.strategies.aggregator`
- `StrategyAggregator(...)` now takes kwargs from `AggregationConfig`, not the model itself
- `aggregate()` returns `list[Signal]`; `main.py` takes the per-market head
- `DataPipeline` got a `(client, ws, event_bus)` constructor, async
  `start`/`stop`, self-subscribes to `orderbook_update`/`trade_update`
- `AlertManager` accepts `AlertsConfig`, has `start`/`stop`
- `Storage.save_order` accepts an `Order` (not just dict); main awaits
  `initialize` / `save_order` / `close`
- `dashboard/app.py` got `create_app(bot, config)` factory
- `DualOutputLogger.isatty` / `fileno` for uvicorn logging
- `config.yaml`: removed `exchange_symbol` (strategies use
  `set_exchange_feed()` instead)
- `pyproject.toml`: declared `aiosqlite>=0.19`

### PR #4 — schema migration

User hit `sqlite3.OperationalError: no such column: ts_unix` on a
pre-existing `features.db`. `FeaturesLogger.__init__` and
`Storage.initialize` now check pre-existing tables against the required
column set; if any are missing, the legacy table is renamed to
`<table>_legacy_<unix_ts>` (rows preserved) before the fresh schema is
created. Compatible DBs are untouched.

### PR #5 — cross-token exit pricing (the catastrophic one)

User's first paper run blew up: SELL @ 0.07 → "exit" @ 0.96 = **−$317
on a $25 position**. Triggered the daily-loss circuit breaker in 2
seconds. Root cause: my PR #3 pipeline stored *one* orderbook per
market and overwrote on every WS frame. Polymarket binary markets have
two tokens with mirror books ~1.0 apart; when the NO frame landed
between fill and exit, exit read NO's ask 0.96 for a position on YES.

Fix: orderbooks now keyed by `token_id`, not market.
`MarketSnapshot.orderbook` is always the primary (first listed) token
regardless of WS update order; new
`DataPipeline.get_orderbook(market_id, token_id)` for the orchestrator;
`main._execute_exit` reads the position's own token's book.
Defence-in-depth: refuses (without `force=True`) to realise an exit
>30% adverse to entry, retries on next cycle. Also dropped the
duplicate `order_filled` emit at `engine.py:211` (carried over failing
test from PR #3).

Bonus: dashboard URL now logs `http://localhost:<port>` (browser-friendly)
instead of `http://0.0.0.0:<port>`.

### PR #6 — dashboard endpoint wiring

`/api/status`, `/api/portfolio`, `/api/exchange-prices` were 500-ing on
every refresh because the dashboard reached for public names that
didn't exist on `Bot`/`ExchangeFeed`. Added the public surface:

- `Bot`: `running`, `config`, `circuit_breaker`, `scanner`, `strategies`,
  `position_manager`, `data_pipeline`, `exchange_feed`, `event_bus`,
  `execution`
- `ExchangeFeed`: `feeds` dict, `last_price`, `last_update`,
  `price_change_pct(s)`, `volatility_window(s)`, `momentum_score()`

All 9 JSON endpoints return 200, zero ASGI exceptions in logs.

### PR #7 — pricing safety + paper P&L reset

User's screenshot showed dashboard opening at −$317.80 from a prior
session, plus a fresh blow-up: maker_edge entered BUY at **1.29**
(impossible — Polymarket prices are `(0, 1)`).

```
inv=-97.66, max_inventory=0.20, inventory_skew=0.5
skew_adj_unbounded = 0.5 × (-97.66 / 0.20) = -244.15
target = 0.075 - 0.004 - 0.005 × -244.15 = 1.29   # ← impossible
```

The ratio was unbounded; existing clamps were one-sided
(`max(0.01, …)` BUY, `min(0.99, …)` SELL).

Three layers of defence:

1. **`strategies/maker_edge.py`** — clamp `inv_ratio` to `[-1, 1]`
   *before* multiplying by skew; hard
   `max(0.01, min(0.99, target_price))` on both branches.
2. **`risk/manager.py`** — `can_place_order` rejects `price ≤ 0` or
   `price ≥ 1` for entry **and** exit with reason
   `price_out_of_range:%.4f`. Final safety net.
3. **`data/storage.py` + `main.py`** — new
   `Storage.clear_session_data()` truncates
   `orders`/`positions`/`pnl_history`. `Bot.start()` calls it after
   `initialize()` *only in paper mode*. Live mode preserves history.
   Dashboard now opens at $0 P&L every paper run.

## Architecture quick reference

```
src/polybot/
├── main.py                       # Bot orchestrator + trading loop
├── config.py                     # Pydantic config models, load_config()
├── events.py                     # async pub/sub EventBus
├── data/
│   ├── client.py                 # Gamma + CLOB; live HARD-GUARDED
│   ├── websocket.py              # Polymarket CLOB WS
│   ├── pipeline.py               # Per-token book buffers, snapshots
│   ├── exchange_feed.py          # Binance BTC spot feed
│   ├── features_logger.py        # SQLite per-cycle feature rows (sync)
│   ├── storage.py                # SQLite orders/positions/pnl (async, aiosqlite)
│   └── models.py                 # Order, Position, Signal, OrderBook, …
├── strategies/
│   ├── aggregator.py             # StrategyAggregator (weighted vote)
│   ├── overshoot_reversion.py    # registered
│   ├── dual_direction_arb.py     # registered
│   ├── boundary_decay.py         # registered
│   ├── maker_edge.py             # registered
│   └── (others: not registered)  # calibration_edge, fair_value, etc.
├── risk/
│   ├── manager.py                # Kelly sizing, can_open/can_place gates
│   └── circuit_breaker.py        # Daily loss halt, alert wiring
├── execution/
│   └── engine.py                 # Paper + live (live not implemented)
├── positions/
│   └── manager.py                # Position lifecycle, exit signals
├── monitoring/
│   └── alerts.py                 # Discord, Telegram, Log channels
├── scanner/
│   └── scanner.py                # BTC up/down market discovery via Gamma
├── diagnostics/
│   └── decision_log.py           # JSONL decision logging
└── dashboard/
    ├── app.py                    # FastAPI routes + WebSocket pushers
    ├── launcher.py               # CLI: bot + uvicorn together
    └── analytics.py              # Edge/risk/signal SQL queries
```

## Known follow-ups

- **`maker_edge` is hyperactive.** It quotes both sides at ±1 tick
  inside the spread and accumulates inventory with each fill.
  Configured `max_inventory: 0.20` is essentially "flatten on first
  fill" given typical Kelly sizing produces 50-100 share orders. Worth
  a tuning pass — bigger `max_inventory`, longer `min_quote_interval_s`,
  or skew-only-when-flat.
- **Live trading not implemented.** `place_order` raises
  `LiveTradingNotImplementedError`. EIP-712 signing is the missing
  piece. Hard guard at `Bot.__init__` refuses unless
  `config.bot.allow_live=True` AND
  `data.client.LIVE_TRADING_ENABLED=True`.
- **Paper trade history wipes on every startup** by design. If users
  want it persisted, add a `--keep-paper-data` flag.
- **Sandbox-only quirk during testing**: HTTP 403 from Polymarket
  Gamma + CLOB WS in this Codespaces-style environment. User-side
  network reaches them fine. Not a code bug.

## Useful invariants

- Polymarket prices live in `(0, 1)`. Anything outside is a bug.
- Binary markets have **two** tokens with mirror books (≈1.0 apart);
  YES = first listed, NO = second.
- `Order.metadata` is propagated from `Signal.metadata` by
  `_build_order` — execution engine reads `is_dual_direction`,
  `no_token_id`, `legs_max_age_ms`, `is_maker_only` from there.
- `MarketSnapshot.orderbook` is the **primary** (YES) token's book
  regardless of WS update order. For exit pricing on the actual
  position, use `pipeline.get_orderbook(market_id, position.token_id)`.

## Files written or significantly touched (cumulative)

```
src/polybot/main.py
src/polybot/data/pipeline.py
src/polybot/data/storage.py
src/polybot/data/exchange_feed.py
src/polybot/data/features_logger.py
src/polybot/dashboard/launcher.py
src/polybot/dashboard/app.py
src/polybot/execution/engine.py
src/polybot/strategies/maker_edge.py
src/polybot/risk/manager.py
src/polybot/monitoring/alerts.py
config.yaml
pyproject.toml
tests/test_pipeline.py            (new)
tests/test_pricing_safety.py      (new)
```

## Merged PRs

- #3 — v4 rewrite drop + integration fixes
- #4 — schema migration for stale features.db / bot.db
- #5 — cross-token exit pricing + dashboard URL + dual-leg test
- #6 — dashboard endpoint wiring (Bot/ExchangeFeed public surface)
- #7 — pricing safety (maker_edge clamp + risk-gate price range + paper P&L reset)
