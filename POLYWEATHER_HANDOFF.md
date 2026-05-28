# PolyWeather-Bot — handoff for next chat session

Drop this whole file into the next chat. Repo lives at `/home/user/BTC-Bot` (Linux sandbox) or
`C:\Users\taitk\OneDrive\Desktop\BTC-Bot-claude-polymarket-bot-design-VFErr` (operator's Windows
machine). Branch in active use: `claude/sweet-clarke-JgvhX`. Default branch:
`claude/polymarket-bot-design-VFErr`.

---

## Who is the operator and what do they want

Frent, 18, UK. £1000 starting bankroll. Wants the bot to **trade actual Polymarket weather
markets**. Runs Windows + VS Code + PowerShell + Python 3.14. Prefers direct list-oriented
communication and honest pushback over encouragement.

The honest envelope for a £1k bot: **£30–£150/month median, £200–£500 top decile, 1-in-4 months
flat or negative.** The £3-5k/month stretch wish is not realistic from £1k in 30 days. Do not
loosen risk parameters to chase that number.

The operator has repeatedly asked for "real trading." The correct answer (and the position to
hold) is: **paper-trade against real Polymarket markets for 14 days, validation gate green, then
real money.** Do not enable real-money trading without validation history.

---

## State of the build (PRs #18 through #25, all merged or open)

Base: `claude/polymarket-bot-design-VFErr`. All work happens on `claude/sweet-clarke-JgvhX`.

| PR | Title | Status |
|----|---|---|
| #18 | Initial PolyWeather-Bot v1 build | merged |
| #19 | Ctrl-C clean exit on Windows | merged |
| #20 | Console status output | merged |
| #21 | Tame mock signal firing rate, fix HALTED-forever bug | merged |
| #22 | SQLite WAL + stale-data latch fix | merged |
| #23 | Hold paper positions to settlement (no instant binary) | merged |
| #24 | Honest mock mode + `--live-data` mode | merged |
| #25 | True live-paper: CLOB orderbook + real-resolution settlement | **OPEN, draft** |

### Repo layout

```
src/polybot/polyweather/
  exchanges/
    polymarket_v2_client.py     V2 SDK wrapper + Mock variant
    gamma_client.py             real-shape parser for gamma-api.polymarket.com
    clob_client.py              REST client for clob.polymarket.com (book/midpoint/price)
    data_api_client.py
    ws_orderbook.py             skeleton, not wired
  data/
    forecasts/
      ensemble_blender.py       handles point + range markets, °C + °F (PR #25)
      nws_client.py             real + mock
      open_meteo_client.py      real + mock (no key needed for free tier)
      met_office_client.py      real + mock (needs MET_OFFICE_API_KEY)
    stations/
      station_resolver.py       parses market rules; audit log (off in mock mode)
      station_catalog.yaml      10 cities; missing several real-Polymarket cities — see Bug E
    climatology/ncei_base_rates.py
  strategies/
    weather_ensemble.py         primary 70%-weight strategy
    negative_risk_arb.py        ← BUG SOURCE — see Bug A
    resolution_meanrev.py
    maker_rebate.py
  risk/
    weather_risk.py             caps, kill switches, ATH/daily/consecutive halt regimes
    validation_gate.py          9 paper→live criteria
  persistence/store.py          SQLite WAL mode + 5s busy timeout
  dashboard/
    routes.py                   7 /api/weather/* endpoints
    frontend/                   index.html + app.js + styles.css
  orchestrator/engine.py        the core loop

scripts/polyweather/
  paper_run.py                  --mock / --live-data
  live_run.py                   gated behind validation gate
  discover_markets.py
  verify_station_mapping.py
  reset_paper_state.py

config/polyweather/
  risk.yaml strategy_weights.yaml markets.yaml

tests/polyweather/  71 tests, all passing as of PR #25
```

### Modes the operator can run today

```powershell
# Synthetic data, $1260 fresh bankroll each run
python scripts/polyweather/paper_run.py --mock

# REAL Polymarket data, REAL forecasts, paper-fill execution (no money)
python scripts/polyweather/paper_run.py --live-data

# Live (refuses to start unless validation gate is green)
BOT_MODE=live python scripts/polyweather/live_run.py --confirm-live
```

---

## Three design decisions you must not undo

1. **Money math uses `Decimal` end-to-end.** Probabilities are `float`. Boundary is the `Signal`
   dataclass.
2. **Per-bucket cooldown** prevents the engine from churning the same market every cycle.
3. **Three halt regimes** — ATH drawdown permanent latch, daily loss 24h cooldown, consecutive
   losses 30-min pause (configurable down for mock).

---

## NEW PROBLEMS (operator just reported, with screenshots)

The operator ran (likely `--live-data`) and reported four interlocking issues:

> "We should have a smooth flow of trades throughout the day, not one batch of trades and the bot
> stops trading even though the daily loss was met.
>
> Sizing is broken. Trades are broken. Risk is broken. Strategies are broken."

### What the screenshots showed

1. **Trade Log** has TWO clusters:
   - **00:43–00:45 AM**: ~15 `weather_ensemble` trades across Paris/Hong Kong/London/Miami/New York
     (an earlier session's opening batch)
   - **20:49–20:50 PM**: **8 `negative_risk_arb` trades on Seoul** all at entry **$0.001**, each
     losing exactly **-$11.99**

2. **Risk tab**:
   - Bankroll: $1164.05 (ATH $1260.00) — down 8% on the day
   - **Max drawdown: 52.74%** intra-day, then partially recovered
   - **Halted: YES — daily_loss_kill_switch: -59.97 <= -50**
   - ALL THREE kill switches ARMED simultaneously:
     - `daily_loss` current `-95.95`, threshold `-50`
     - `ath_drawdown` current `0.527`, threshold `0.20`
     - `consecutive_losses` current `8`, threshold `5`
   - P&L by strategy: `negative_risk_arb -$95.95`, `weather_ensemble +$921.16`

3. **Overview tab**:
   - Bankroll: $1164.05, 24h P&L: $0.00 (rolled over UTC day)
   - Open Positions: 0
   - Equity curve: drops to $1164 then **flat for the rest of the day** because daily_loss cooldown
     is 24h

---

## Root-cause analysis + suggested fixes

### 🐛 Bug A — `negative_risk_arb` fires the entire basket every cycle

`src/polybot/polyweather/strategies/negative_risk_arb.py` returns a list of signals, one per bucket
of the event. The trade log shows **8 arb trades on Seoul** within ~1 minute at the same $0.001
entry. Cooldown should apply per-bucket but appears not to be working for arb signals, OR the
strategy is finding the basket mispriced repeatedly across many buckets.

**Fix (PR-A)**: at the engine level add `_last_event_arb_fired_ts: dict[event_id, float]` and skip
arb evaluation entirely for an event if it fired in the last hour. Cheaper alternative: in the arb
strategy itself, return at most ONE signal (the highest-edge bucket) per event per call.

### 🐛 Bug B — Sizing always hits the $12 cap on tail trades

Every losing trade is exactly **-$11.99**. That's `$12 × (1 − $0.001)/$1 ≈ $11.99` — the 1% cap is
binding because Kelly wants huge positions on 8000+ bps edges at long-shot prices.

Kelly-correct mathematically, but **fat-tail blow-up risk is not in the standard Kelly formula**.
A $0.001 bet has a 999:1 payoff and 99.9% loss probability for the market price — but the bot
treats it as a 800bps edge over the model and bets the cap. With 8 of these in a row, you lose
$96.

**Fix (PR-B)**: scale the cap down on tail prices.
```python
# In weather_risk.py::quarter_kelly_size:
tail_discount = min(Decimal("1"), target_price / Decimal("0.05"))
cap = (self.weather_position_cap_usdc() * tail_discount).quantize(Decimal("0.0001"))
```
A $0.001 bet would size to `$12 × 0.02 = $0.24`, not $12. Add a unit test pinning behaviour at
$0.001, $0.01, $0.05, $0.10.

### 🐛 Bug C — Bot halts and stays halted for the whole 24h day

In `--live-data` mode `daily_loss_cooldown_seconds = 86400` (per spec). Hit the daily loss in the
first hour → sit halted for 23 hours doing nothing. Operator wants "smooth flow of trades
throughout the day."

This is partially **correct** behavior — the spec says daily loss = 24h cooldown. But:
1. The dashboard doesn't say "next attempt in 23h45m" — UX is opaque.
2. The bot shouldn't be hitting the daily loss in the first place — that's a sign Bug A + Bug B
   are draining the bankroll too fast. Fix those first.

**Fix (PR-C)**: dashboard `/api/weather/risk` returns `halt_recovery_in_seconds` computed as
`(halt_started_ts + cooldown) - now`. Render in Risk tab as "next attempt in HHmm".

### 🐛 Bug D — Per-strategy concentration unenforced

P&L split: `weather_ensemble +$921` (from an earlier session), `negative_risk_arb -$95`. The
current session is entirely arb losses. The 70/20/10 weight in `strategy_weights.yaml` is meant to
**limit** capital per strategy but nothing enforces it — every strategy candidate competes equally
in the engine's `max_signals_per_cycle` slot.

**Fix (PR-D)**: in `WeatherRiskManager` track `open_exposure_by_strategy: dict[str, Decimal]`. Add
`strategy` parameter to `can_open`; refuse if `projected + current > weight × bankroll`. Update
engine `_handle_signal` to pass `signal.strategy`. So `negative_risk_arb` could never have more
than `0.20 × $1260 = $252` in open exposure.

### 🐛 Bug E — Station catalog missing real-Polymarket cities

The operator's Polymarket screenshot shows weather markets for: London, Miami, Hong Kong,
Amsterdam, Chicago, Seoul, Denver, Paris, Tokyo, Mexico City. My catalog only has 10 cities and
**misses Amsterdam, Denver, Tokyo, Mexico City**. Real markets for those resolve via the
`station_resolver` returning `None` → engine skips → no trading on them.

**Fix (PR-E)**: add to `src/polybot/polyweather/data/stations/station_catalog.yaml`:
- `EHAM` (Amsterdam Schiphol) — aliases: Amsterdam, AMS
- `KDEN` (Denver International) — aliases: Denver, DEN
- `RJTT` (Tokyo Haneda) — aliases: Tokyo, Haneda, HND
- `MMMX` (Mexico City Benito Juárez) — aliases: Mexico City, MEX
- Also add `EGLL` (Heathrow) as an alias for `EGLC` since real markets sometimes reference Heathrow

### 🐛 Bug F (subtle) — Real-resolution mid-as-outcome inference

In PR #25 `_check_real_resolutions` infers outcome from `CLOB.fetch_midpoint > 0.5`. After a market
closes the midpoint may have already drifted toward 0 or 1 but it isn't authoritative — the
**on-chain CTF outcome** is. For v1 the inference is acceptable but flag it for an eventual fix
that reads the actual `ConditionalTokens.payoutDenominator` / `payoutNumerators` from Polygon.

---

## How to reproduce + debug

```bash
cd /path/to/repo
python scripts/polyweather/paper_run.py --mock --duration 60 --bucket-cooldown 10 \
  --status-every 5 --port 18888

# In another shell, inspect trades by strategy
python -c "
import sqlite3
db = sqlite3.connect('data/runtime/polyweather/paper.sqlite')
for s, n, total in db.execute(
    'SELECT strategy, COUNT(*), SUM(CAST(realised_pnl_usdc AS REAL)) FROM trades GROUP BY strategy'
).fetchall():
    print(f'{s}: {n} trades, total \${total:.2f}')
print()
print('Recent 10 trades:')
for r in db.execute(
    'SELECT closed_at, strategy, city, entry_price, exit_price, realised_pnl_usdc '
    'FROM trades ORDER BY closed_at DESC LIMIT 10'
).fetchall():
    print(r)
"
```

If you see >2 `negative_risk_arb` trades in the same event within 60 seconds, **Bug A is
reproduced**. If every losing trade is `-$11.99` regardless of entry price, **Bug B is reproduced**.

---

## Suggested PR plan

Ship one PR per bug. Each should be small enough to review in 5 minutes.

| PR | Bug | Files touched | Tests to add |
|----|-----|---------------|--------------|
| PR-A | Arb basket spam | `engine.py` or `negative_risk_arb.py` | event-cooldown integration test |
| PR-B | Tail-price sizing | `weather_risk.py`, `test_risk_manager_weather.py` | parametrise sizing at $0.001/0.01/0.05/0.10 |
| PR-C | Halt countdown UX | `dashboard/routes.py`, `frontend/app.js` | new field in `/api/weather/risk` response |
| PR-D | Per-strategy exposure cap | `weather_risk.py`, `engine.py` | exposure-cap rejection test |
| PR-E | Catalog cities | `station_catalog.yaml` only | extend `test_station_resolver.py` fixtures |

**Do PR-A and PR-B first** — they stop the bankroll bleed. PR-C is UX. PR-D is hardening. PR-E
extends coverage.

---

## What's working — do NOT touch

- 71/71 tests pass on the head of `claude/sweet-clarke-JgvhX`
- `ruff check` clean
- Mock-mode end-to-end smoke test runs in ~5s
- `--live-data` mode banner + warning strip in dashboard
- Auto-reset on `--mock`, preserve state on `--live-data`
- WAL mode + 5s busy timeout on SQLite (no more "database is locked")
- Ctrl-C clean exit on Windows (no traceback)
- Validation gate (9 criteria) correctly evaluates and gates `live_run.py`
- Heartbeat task running in its own asyncio.Task
- EIP-712 domain version is the string `"2"`, batch size 15
- Real-resolution settlement when markets close on Polymarket (PR #25)

---

## Useful commands

```powershell
# Run all tests
python -m pytest tests/polyweather -q

# Lint
ruff check src/polybot/polyweather scripts/polyweather tests/polyweather

# Mock smoke (5 seconds)
python scripts/polyweather/paper_run.py --mock --duration 5 --cycle-seconds 1 --port 18888

# Live-data short run
python scripts/polyweather/paper_run.py --live-data --duration 120 --port 18888

# Reset paper state
python scripts/polyweather/reset_paper_state.py
# Or manually:
del data\runtime\polyweather\paper.sqlite*

# Suppress structlog INFO noise
python scripts/polyweather/paper_run.py --mock --log-level WARNING
```

---

## Environment notes

- **Linux sandbox** (where the AI runs) has **no external network** — cannot reach
  `gamma-api.polymarket.com` or `clob.polymarket.com`. All live-data testing happens on the
  operator's Windows machine.
- Python 3.11 in the sandbox; operator runs 3.14. Some libraries (e.g. scipy) may have wheel
  issues on 3.14 — operator should fall back to 3.12 if `pip install` fails.
- Polymarket V2 went live April 28, 2026. EIP-712 domain version is the string `"2"` (not int 2).
  Batch order limit is 15 (not 5). Fees must be fetched per-token via `/fee-rate?token_id=X`.

---

## Briefing protocol before doing anything

1. Read this file
2. `git log --oneline -20` to see recent commits
3. `git diff origin/claude/polymarket-bot-design-VFErr...HEAD --stat` — what's on the branch vs
   default
4. Read https://github.com/tomydabest-sys/BTC-Bot/pull/25 — current open PR
5. Run `python -m pytest tests/polyweather -q` to confirm baseline (should be 71 passing)
6. Pick ONE bug (A through E) and ship one focused PR

Do **not** start by writing a new design document. Do **not** rewrite anything that's already
working. Fix the specific bugs the operator reported. Commit + push + open the PR yourself.

---

## The operator's last message verbatim

> "We should have a smooth flow of trades throughout the day, not one batch of trades and the bot
> stops trading even though the daily loss was met.
>
> Sizing is broken.
> Trades are broken.
> Risk is broken.
> Strategys are broken.
>
> Analyse the images attached and fix the issues you spot aswell as what ive listed."

Take it seriously. The bugs are real and the analysis above is the starting point. Do not
patronise the operator about what's "expected behavior" without first checking whether the bug
they're seeing is actually a bug (Bugs A, B, D definitely are).
