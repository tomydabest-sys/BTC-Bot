# BTC-Bot v4 Rewrite — Session Handoff

Repo: `tomydabest-sys/BTC-Bot` · default branch:
`claude/polymarket-bot-design-VFErr` · working branch: `v4-rewrite`
(everything below has already been merged into default).

## Live performance — 2026-05-09 paper run (14 round-trips in hour 1, then silence)

Bot ran for **8h08m** in paper mode. Trade log shows **14 round-trips**,
**all clustered in the first hour**, then 7+ hours of zero new activity
despite the dashboard reporting `RUNNING`. The performance during the
trading window was actually solid — the open question is now *why
trading stopped*.

### Headlines (the 14 trades)

| Metric | Value | Read |
| --- | --- | --- |
| Total P&L | **+$18.45** | profitable session |
| Win rate | 7W / 7L = **50%** | coin flip |
| Avg win | **$4.39** | … |
| Avg loss | $1.76 | … 2.5× smaller than avg win |
| Profit factor | 30.76 / 12.31 = **2.50** | **good** (>1.5 is the bar) |
| Largest win | #6: BUY 0.210 → 0.430 = **+$15.29** in 58s | |
| Largest loss | #5: SELL 0.140 → 0.240 = −$6.38 in 50s | |
| Position size range | $3 → $69 | wider than prior run |
| Hold-time range | 2s → 5m | much tighter than prior run |

### What's working

- **Asymmetric payoff!** Avg win = 2.5× avg loss. The strategies are
  catching real moves when they fire — exactly the profile a
  break-even win-rate system needs to be profitable.
- **Big wins on low-priced BUY entries.** #3 (BUY 0.110 → 0.370,
  +$4.73), #4 (BUY 0.080 → 0.280, +$5.00), #6 (BUY 0.210 → 0.430,
  +$15.29), #8 (BUY 0.230 → 0.690, +$4.00). Buying YES cheap and
  catching a rally pays out with the same asymmetry that makes
  out-of-money options profitable.
- **Pricing safety still holds.** Every entry / exit ∈ `[0.08, 0.99]`.
  No 1.29 fills, no cross-token blow-ups.
- **Auto-close working.** #10 and #14 closed via `auto_close` before
  resolution.
- **No catastrophic single trade.** Worst loss was −$6.38 vs +$15.29
  best win.

### What's not working

#### 1. Trading died after ≈hour 1 (the new headline issue)

Uptime says 8h08m, last update is recent (19:01:40), bot says
`RUNNING`. But trade #14 (the most recent) is at least **7 hours
ago**. Possible causes, in order of likelihood:

1. **Market discovery stalled.** Scanner loaded the initial batch of
   BTC up/down markets, the bot traded them out, and either:
   - the resolution-window filter (`scanner.resolution_window_days:
     [0, 7]`) is too narrow once "now" advances,
   - new markets aren't being parsed (Gamma API schema drift,
     timestamp slugs going stale),
   - or `_force_closed_markets` is permanently dedupe-blocking them.
2. **Positions stuck open.** If exit pricing is being sanity-blocked
   for a position whose underlying market has closed, the position
   never clears, eventually consumes the position-count cap, and
   `can_open_position` rejects every new entry.
3. **WebSocket silently dead.** No `book` events → no orderbook
   updates → snapshots return None → strategies can't evaluate.
4. **Circuit breaker tripped on something other than daily loss.**
   `consecutive_losses_pause` is 8; we had ≤4 consecutive losses, so
   probably not — but check.

**First diagnostic to run:** grep the log for `scan_complete`,
`btc_market_found`, `ws_book_event`, and `exec_blocked` after the
1-hour mark. The pattern of what stopped tells you which of the four
above is the culprit.

#### 2. The fast-exit losses are still leaking

| # | Side | Entry | Exit | P&L | Hold |
| --- | --- | --- | --- | --- |
| #1 | SELL | 0.100 | 0.120 | −$1.25 | **2s** |
| #2 | BUY | 0.110 | 0.100 | −$0.18 | **8s** |

`maker_adverse_selection` (or similar exit) firing on the very first
MTM tick — same pattern as the prior run. Adds ~$1.40 of bleed per
session for nothing.

#### 3. Sizing is now hitting the configured caps

#5 ($64), #6 ($69), #1 ($63) all exceed `max_position_size: 50` from
the config. Either the cap isn't being enforced for entry sizing, or
these are accumulated multi-fill positions where the cap is checked
only on the marginal order. Worth investigating —
`max_position_size` should bound the *total notional*, not the order.

#### 4. Still no strategy attribution in the Trade Log

Same point as before: every exit reads `stop_loss` / `auto_close`,
none surface `position.strategy`. Without that, can't attribute the
+$15.29 on #6 vs the −$6.38 on #5 to a specific strategy.

### Suggested next steps (impact-ordered)

1. **Diagnose the trading-stalled state.** Pull the bot log,
   `grep -E 'scan_complete|btc_market_found|ws_book_event|exec_blocked|markets_fetched'`
   from ~hour 1 onwards, look for what stopped. Most likely the
   scanner isn't finding new markets after the initial batch resolved.
   This is the single highest-leverage thing — a profitable strategy
   that can't keep trading is worthless.
2. **Add strategy attribution to the Trade Log row** (still
   outstanding from the prior performance review). Pure dashboard
   change. Unlocks per-strategy P&L splits — we'd immediately see
   whether #6's +$15.29 is `overshoot_reversion` or
   `dual_direction_arb`.
3. **Plug the 2-second exits.** Add a 5-second minimum hold before
   exit checks fire on a fresh fill. Saves ~$1-2 per session for
   zero downside.
4. **Audit the position-size cap.** Confirm `max_position_size: 50`
   is enforced against total notional after a fill, and that the
   $64-$69 positions in #5/#6 are intentional (e.g. dual-leg
   combined notional) and not a sizing-gate bypass.

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
