# BTC-Bot v4 Rewrite — Session Handoff

Repo: `tomydabest-sys/BTC-Bot` · default branch:
`claude/polymarket-bot-design-VFErr` · working branch: `v4-rewrite`
(everything below has already been merged into default).

## Live performance — 2026-05-09 paper run (20 round-trips, 1h29m uptime)

After PR #7 the bot ran a full paper session. Trade log captured 20
round-trips. Pricing safety holds end-to-end (no impossible entries, no
catastrophic single-trade losses). The remaining issue is strategy
quality, not bot integrity.

### Headlines

| Metric | Value | Read |
| --- | --- | --- |
| Total P&L | **−$0.64** | bleeding slowly, not catastrophic |
| Win rate | 10W / 10L = **50%** | coin flip |
| Avg win | $0.79 | ≈ avg loss |
| Avg loss | $0.85 | … so no edge |
| Profit factor | 7.90 / 8.54 = **0.92** | <1.0 = losing on average |
| Trades / hour | ≈13 | reasonable cadence |
| Hold-time range | 1s → 55m | bimodal — see #3 below |
| Position size range | $2 → $26 | bounded, no runaways |

### What's working

- **Pricing safety:** every entry / exit ∈ `[0.02, 0.99]`. The
  cross-token pipeline fix + maker_edge clamp + risk-gate
  `price_out_of_range` are all doing their job. No more 1.29 entries.
- **Auto-close before expiry:** trades #3, #12, #19 closed via
  `auto_close` before resolution — the safety net fires.
- **Some genuine mean-reversion wins:**
  - #6: SELL 0.460 → 0.210 = **+$1.59** in 53s
  - #9: SELL 0.450 → 0.160 = **+$1.93** in 1m
  Both are >+30% on the position; look like overshoot_reversion catching
  a real reversal.
- **Sizing bounded** at $2–$26, no runaway positions.

### What's not working

#### 1. Asymmetric BUY-side losses on mid-range entries

| # | Side | Entry | Exit | P&L | Hold |
| --- | --- | --- | --- | --- | --- |
| #7 | BUY | 0.450 | 0.110 | −$1.51 | 1m |
| #8 | BUY | 0.460 | 0.190 | −$1.17 | 1m |
| #10 | BUY | 0.440 | 0.080 | **−$1.96** | 1m |
| #17 | BUY | 0.880 | 0.820 | −$0.71 | 36s |

#10 is a 0.440 → 0.080 move in a minute (BTC moving sharply against the
prediction). Labelled `stop_loss` but the price moved 36¢ before the
strategy could pull out. Either the stop threshold is too loose, the
exits-loop interval (1s polling) is too slow, or these are dual-leg
arb entries marked-to-market against an already-moved book.

#### 2. Symmetric profile = no edge

`avg_win ≈ avg_loss` at 50% win rate is the textbook signature of
"strategies have no edge after costs." Wins (#6, #9, #12) and losses
(#7, #8, #10) are roughly the same magnitude. There's no asymmetric
payoff being captured.

#### 3. 1-second exits

#13: SELL 0.760 → 0.800 in **1 second** = −$1.05. The exit loop is
firing on essentially the same tick as entry — likely
`maker_adverse_selection` triggering on the very first MTM after fill.
That's a "fees + slippage" trap, not a real signal.

#### 4. No strategy attribution in the Trade Log

Every exit is labelled `stop_loss` / `auto_close` but never which
strategy entered. Without `position.strategy` in the log row, can't
tell whether #10's −$1.96 came from `dual_direction_arb`,
`maker_edge`, or `overshoot_reversion`.

#### 5. `maker_edge` still hyperactive

Even with the price clamp, earlier logs showed it re-firing every ~3s
with `inv` swinging ±100 shares. Churning through the spread + paper
fees. Probably needs `min_quote_interval_s` raised and `max_inventory`
reset to a sane multiple of typical Kelly sizing.

### Suggested next steps (impact-ordered)

1. **Add strategy attribution to the Trade Log row.** Pure dashboard
   change in `dashboard/app.py` and the analytics SQL. ~5 min, unlocks
   per-strategy analysis.
2. **Investigate the 1-second exits.** Either tighten
   `maker_adverse_selection` threshold, add a minimum hold (~5s) before
   exit checks fire, or skip the exits cycle for the first N seconds
   after a fill.
3. **Try disabling `maker_edge` for one session.** If P&L improves,
   maker_edge is dragging. If it stays flat, the bleed is
   `dual_direction_arb` / `overshoot_reversion` paying for inferior
   fills.
4. **Tighten the stop on BUY entries above 0.40.** A 0.44 → 0.08 move
   in 1 minute means the existing stop didn't fire fast enough; high
   absolute-price BUY entries have asymmetric downside.
5. **Verify `dual_direction_arb` resolution math.** Confirm both legs
   are being placed and the recorded P&L is for the *combined* arb,
   not just one leg.

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
