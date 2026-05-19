# BTC-Bot Phase 0 Audit

Baseline run captured against branch `claude/review-btc-bot-codebase-enE8D` on 2026-05-19.

## 1. Test suite baseline

`pytest tests/ -v`

- Before this work: **172 passed, 2 failed**
  - `test_telegram_alerts.py::test_critical_alerts_dedup_within_window`
  - `test_telegram_alerts.py::test_distinct_alert_keys_send_separately`
- Root cause: `TelegramAlerter._send_once` used `0.0` as the
  "never sent" sentinel and applied the 300 s dedupe window to the
  first send as well. When `time.monotonic()` is small (early in
  process life) the very first critical alert was silently swallowed.
- Fix: distinguish "never sent" (`dict.get` returning `None`) from
  "sent recently" before applying the dedupe window.
- After fix: **174 passed, 0 failed**.

## 2. `python diagnose_v2.py` output

```
========================================================================
  BTC-BOT v2 DIAGNOSTIC
========================================================================
  Time: 2026-05-19T16:10:10.760675
  CWD:  /home/user/BTC-Bot

[1. ORDERS IN bot.db]
------------------------------------------------------------------------
  [!] data/bot.db not found

[2. DECISION LOG — last 5 minutes]
------------------------------------------------------------------------
  Total decisions in last 5 min: 34

  By decision:
    BLOCKED              30       (88.2%)
    BUY                  2        (5.9%)
    SELL                 2        (5.9%)

  Per-strategy decision mix:
    risk(overshoot_reversion)    total=10     ok=0     (0.0%) blocked=10
    maker_edge                   total=6      ok=4     (66.7%) blocked=2
    exec(overshoot_reversion)    total=4      ok=0     (0.0%) blocked=4
    overshoot_reversion          total=4      ok=0     (0.0%) blocked=4
    boundary_decay               total=4      ok=0     (0.0%) blocked=4
    exec(dual_direction_arb)     total=2      ok=0     (0.0%) blocked=2
    risk(dual_direction_arb)     total=2      ok=0     (0.0%) blocked=2
    dual_direction_arb           total=2      ok=0     (0.0%) blocked=2

  Top block reasons:
    time_remaining_too_high             6        (20.0%)
    kill_switch                         4        (13.3%)
    risk_block                          4        (13.3%)
    position_cap                        4        (13.3%)
    no_signal                           2        (6.7%)
    no_feed                             2        (6.7%)
    daily_loss_halt                     2        (6.7%)
    ovr_01_feed_warming                 2        (6.7%)
    ovr_05_no_burst                     2        (6.7%)
    time_remaining_too_low              2        (6.7%)

[3. RECENT LOG ACTIVITY]
------------------------------------------------------------------------
  [!] logs/polybot.log not found

[4. V2 PATCH VERIFICATION]
------------------------------------------------------------------------
  [v2 OK]      src/polybot/strategies/maker_edge.py  ::  'max_position_notional_usd'
  [v2 OK]      src/polybot/strategies/maker_edge.py  ::  'min_quote_interval_s'
  [v2 OK]      src/polybot/risk/manager.py  ::  '_is_exit_order'
  [v2 OK]      src/polybot/data/websocket.py  ::  'MAX_TOKENS_PER_SUBSCRIBE'
  [v2 OK]      src/polybot/data/websocket.py  ::  '_send_subscribe_batch'
  [v2 OK]      src/polybot/main.py  ::  '_force_closed_markets'
  [v1 OR BAD]  src/polybot/main.py  ::  'market_expired_force_close'

[5. ACTIVE CONFIG SUMMARY]
------------------------------------------------------------------------

  config.aggressive.yaml:
    bankroll_usd                 500
    kelly_fraction               0.50
    edge_floor_bps               1
    min_spread                   0.005
    confidence_floor             0.35
    max_time_remaining           285.0
    min_overshoot                0.003
    loop_interval_ms             500

  config.yaml:
    bankroll_usd                 500
    kelly_fraction               0.50
    edge_floor_bps               3
    min_spread                   0.008
    confidence_floor             0.40
    max_time_remaining           285.0
    min_overshoot                0.005
    loop_interval_ms             500

[6. ENVIRONMENT]
------------------------------------------------------------------------
  BOT_FORCE_TRADE           = (unset)
  BOT_MODE                  = (unset)
  BOT_LOG_LEVEL             = (unset)

========================================================================
  END DIAGNOSTIC — paste this entire output back to chat
========================================================================
```

The "v1 OR BAD" hit on `market_expired_force_close` is a stale
substring check in `diagnose_v2.py`; the actual semantics (force-closing
positions in markets nearing expiry) live in
`Bot._auto_close_loop` + `Bot._force_closed_markets`, which the patch
verifier flags green. No follow-up needed.

## 3. SESSION_HANDOFF "Suggested next steps" status

| # | Item | Already fixed? | If not, proposed approach |
|---|------|----------------|---------------------------|
| 1 | Add strategy attribution to the Trade Log row. | **Partial.** `_pair_trades` already preserves `entry.get("strategy", …)` on the `TradePair` and `compute_signal_analysis` exposes a `by_strategy` bucket. The dashboard `recent_trades` JSON includes `strategy`, but the Trade Log row in `index.html` does **not** render it (grid is `# / SIDE / ENTRY / EXIT / SIZE / P&L / HOLD-EXIT`). | Add a STRATEGY column to the Trade Log grid (Phase 1) + surface a dedicated `per_strategy` analytics block (full edge metrics bucketed by entry strategy) on the Signals tab. |
| 2 | Investigate the 1-second exits. | Not fixed. No minimum-hold gate or maker-adverse-selection cooldown was introduced. | Out of scope for this gate-hardening pass; flagged for the next iteration. The maker pivot (Phase 2) replaces `maker_edge` with the QuoteManager-based stack which has its own staleness threshold (`requote_threshold_cents`) and lifetime ceiling — that should subsume the bug class. |
| 3 | Try disabling `maker_edge` for one session. | Operator action, not code. | `config.maker_paper.yaml` ships in Phase 2 with `strategies.enabled: []` so the V2 maker stack runs without the legacy taker strategies, providing exactly this isolation. |
| 4 | Tighten the stop on BUY entries above 0.40. | Not fixed. | Out of scope for this pass; tracked under "Phase 5 future work" — the V2 maker stack avoids open-ended directional BUY entries entirely. |
| 5 | Verify `dual_direction_arb` resolution math. | Not fixed in code. Tests cover only the entry path. | Will be covered indirectly once Phase 3 wires real round-trip P&L into the validation gate; until then, dual-arb is left as-is. |

## 4. V2 subsystem wiring status

Source: `src/polybot/main.py` (Bot.__init__ + Bot.start).

| Subsystem | Wired? | Notes |
|-----------|--------|-------|
| `MakerOrchestrator` (`config.maker.enabled`) | **Wired conditionally.** Constructed when `config.maker.enabled` is True; `start()` is awaited from `Bot.start`. Default `config.yaml` ships with `maker.enabled: false`, so it is OFF by default. | Phase 2 ships `config.maker_paper.yaml` with `maker.enabled: true`. |
| `PaperValidationGate` | **Wired conditionally.** Constructed alongside MakerOrchestrator. State persisted on shutdown via `_stop_maker_subsystem`. | Auto-save loop + daily-returns ticker missing; addressed in Phase 3. |
| `TelegramAlerter` | **Wired conditionally.** Same gating as the gate. Heartbeat task started in `Bot.start`, stopped in `Bot.stop`. | OK. |
| ATH drawdown kill switch (`config.risk.ath_drawdown_kill_pct`) | **Defined but unwired.** `RiskManager.record_equity` exists and the gate logic in `can_open_position` checks `_ath_killed`, but no caller invokes `record_equity` anywhere in the running bot. The threshold defaults to `0.0` (disabled) and even when set the kill switch can never trip because peak equity is never updated. | Phase 4 wires it into `_status_log_loop` so equity is sampled once per minute. |

## Post-Phase summary (Phases 1-4 outcome)

| Phase | Outcome |
|-------|---------|
| 1 | `compute_per_strategy_breakdown` added to `dashboard/analytics.py`; surfaced under `per_strategy` in `/api/analytics`; new PER-STRATEGY EDGE panel in `dashboard/frontend/index.html`; Trade Log grid gained a STRATEGY column; tests in `tests/test_dashboard_analytics.py`. |
| 2 | `config.maker_paper.yaml` shipped (maker ON, taker stack empty, ATH-kill armed). Existing maker integration test exercises the wiring under the mock BTC feed; new regression test loads the file and asserts MakerOrchestrator + PaperValidationGate are constructed. |
| 3 | `_RoundTripTracker` (FIFO lot matcher) added to `execution/maker_orchestrator.py`; `_paper_fill_sweep` now records real round-trip P&L on close instead of spamming gross=0; daily-returns ticker + 5-min auto-save loops started on MakerOrchestrator when the gate is wired. Tests in `tests/test_paper_validation.py`. |
| 4 | `Bot._emit_status` now feeds equity into `RiskManager.record_equity` every minute (the ATH-kill switch is finally on-line) and best-effort persists the validation gate. `scripts/validation_status.py` ships with exit codes `0=READY / 1=NOT_READY / 2=INSUFFICIENT_DATA`. README has a new "Paper Validation Run" section describing the workflow + the meaning of each gate metric. |

## 5. Phase 0 exit criteria

- [x] `pytest tests/ -v` is green (174/174 after the telegram fix).
- [x] `diagnose_v2.py` runs and is captured in this file.
- [x] SESSION_HANDOFF items audited.
- [x] Subsystem wiring status documented.

Bot state in one paragraph: a fresh paper-mode start drains
`bot.db`, launches the legacy taker stack (`overshoot_reversion`,
`dual_direction_arb`, `boundary_decay`, `maker_edge`) plus dashboard,
and runs a 500 ms trading loop with Kelly sizing, ATH-kill scaffolding,
auto-close-before-expiry, settlement backfill, and per-strategy
decision logging. The V2 maker stack (MakerOrchestrator + QuoteManager
+ InventoryManager + PaperValidationGate + TelegramAlerter) is fully
implemented and unit-tested but lives behind `config.maker.enabled`,
which defaults to `false`. The 30-day validation gate evaluates 9
hard-coded thresholds against a `ValidationState` it persists to JSON,
but the production code path currently records `gross_pnl_usd=0.0`
on every paper fill, never calls `record_daily_return`, only saves on
shutdown, and never feeds equity into the ATH kill switch. Phases 1-4
of this brief close those gaps.
