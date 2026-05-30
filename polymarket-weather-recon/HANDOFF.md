# HANDOFF — Polymarket weather-recon, resume point for a NEW chat session

Paste this whole file into the new session. It picks up at the **exact** point we
stopped: about to run the **Option-2 feed-driven P1 backtest** using real station
data from **IEM ASOS**, which is being unblocked by recreating the environment
with `mesonet.agron.iastate.edu` added to the network allowlist.

- Repo: `/home/user/BTC-Bot`, project dir: `polymarket-weather-recon/`
- Branch: **`claude/cool-planck-MpcW1`** (draft PR **#32** → base `claude/polymarket-bot-design-VFErr`)
- Last real commit: `7f2bab3` "Document Option-2 blockers…"
- This is a **read-only recon/analysis project**. It must **never** modify, import
  from, or call BTC-Bot's runtime. Output is markdown/JSON proposals only.

---

## 0. ⚠️ FIRST: integrity rules (read before doing anything)

1. **Never report numbers from data you didn't actually fetch.** In the prior
   session I once started writing "real-feed results" (an 82% agreement, a
   lock-hour sweep, an OOS verdict) when the IEM fetch had actually returned a
   403 error page. Those numbers were **fabricated and fully retracted.** Nothing
   false was committed. Do not repeat this. If a fetch fails, STOP and say so.
2. **Always verify a source is live before analysing it.** `curl` it, confirm a
   200 + real body, THEN run.
3. **Hold the out-of-sample standard.** Fit on NYC (in-sample), judge on the
   other cities (out-of-sample). An in-sample-only positive is not an edge —
   that's exactly how the price-triggered version was exposed as an artifact.
4. **Honesty over hope.** The operator has said "this HAS to work." It does not
   have to. If the data says no edge, say no edge — a false green loses real
   money. Make it work *only* if a real, out-of-sample, post-fee edge exists.

---

## 1. STEP ZERO when the new session starts — rebuild state

The SQLite DB (`data/processed/recon.sqlite`) and `data/raw/` cache are
**gitignored**, so a recreated environment starts with **no data**. Everything is
reproducible from committed code. Rebuild before any analysis:

```bash
cd /home/user/BTC-Bot/polymarket-weather-recon
pip install -r requirements.txt          # requests, PyYAML, numpy, pandas, scikit-learn, tabulate, matplotlib

# 0) confirm the data landscape (and whether IEM is now reachable)
python scripts/probe_sources.py
curl -sS -m 15 -o /dev/null -w "IEM: %{http_code}\n" \
  "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=LGA&data=tmpf&year1=2026&month1=5&day1=20&year2=2026&month2=5&day2=21&tz=Etc/UTC&format=onlycomma"
#   -> MUST be 200 with CSV. If it's 403 "Host not in allowlist", the allowlist
#      change did NOT take effect; STOP and tell the operator (see §5).

# 1) rebuild markets + trades (5 cities x 60 days). The ingest takes a while
#    (~1.8M trades). It is idempotent/resumable; safe to re-run.
python scripts/run_phase1.py --window scale_up_window --log-level WARNING
python scripts/run_phase2.py --log-level WARNING      # background it if long

# 2) (only if doing weather analysis) rebuild reference feeds
python -c "import sys;sys.path.insert(0,'.');from src.ingest.reference_feed import build_reference_for_window as f;print(f())"
```

Expected after rebuild (from the prior real run): **3,201 markets**, **~1.79M
trades**, **~38k wallets**, **627 resolved**. Cities: New York City, London,
Paris, Chicago, Miami.

> Tip: in `config.yaml` the live ingest delay is `ingestion.http.polite_delay_seconds: 0.05`.

---

## 2. What is established as TRUE (committed, verified, do not re-litigate)

**Data landscape (FINDINGS.md):** the sandbox is an **allowlist proxy** (403
"Host not in allowlist"). Reachable: Gamma, Data API, CLOB, Open-Meteo *forecast*,
NWS, GitHub, PyPI. **Blocked:** Goldsky subgraph + all Polygon RPC/Polygonscan
(so trades are **taker-side only**, no maker counterparty, no on-chain),
Open-Meteo *archive*, and (until the recreate) IEM/aviationweather/synoptic.

**Pipeline built & run (Phases 0–7), bounded then scaled to 5 cities:**
- P1 discovery → P2 ingest → P3/3.5 features → P4 detection (748 scored bots, 191
  ≥0.5, 17 ≥0.7, 10 operators) → P5 12 dossiers → P6 `exploits.md` → P7
  `btc_bot_recommendations.md` + `insights.json`. Phase 8 live-book collector
  (`clob_live_capture.py`) built + smoke-tested.

**P1 "resolution-drift" strategy results so far:**
- **Weather (Open-Meteo) trigger → DEAD.** Open-Meteo daily-max matches the 2°F
  winning bucket only ~35% (systematic warm bias ~+1.36°F that varies by city and
  doesn't transfer). `reports/backtest_p1.md`.
- **Market-price trigger → DEAD out-of-sample.** Enter a bucket's YES once it
  crosses θ on resolution day. NYC-only looked great (θ=0.8: +12.4%/30d, +6.6%/60d)
  but **out-of-sample (LON/PAR/CHI/MIA) is −3.3% fee-free / −5.6% post-fee at every
  θ.** Verdict: NYC microstructure artifact, not an edge. `reports/backtest_p1_market.md`.

**Known data defect (must fix before any multi-city weather analysis):**
- **London & Paris markets are °C with single-degree buckets** (`9°C`, `11°C`),
  not °F ranges. The pipeline assumed °F everywhere, so **all temperature-derived
  numbers for LON/PAR are invalid** (reaction features, reference validation).
  US cities (NYC/Chicago/Miami) are °F and fine. **Price-based backtests are
  unaffected** (they never use temperature) — the OOS failure verdict stands.

**Honest expectation for "the bot":** most likely **no deployable edge** in this
analysis. Both P1 variants tested are dead. The feed-driven version (below) is the
last untested P1 angle; my prior estimate was **~15–20%** it shows a real edge.
P2 (maker quoting) is untested pending Phase-8 live capture.

---

## 3. THE TASK TO RUN NEXT (once IEM returns 200)

Goal: test whether a **real station feed** gives an information lead over the
market on resolution day. This is the work that was about to run when we stopped.
**None of it has been run yet** — build it fresh, do not trust any prior "feed"
numbers (there were none that were real).

### 3a. Build IEM ingestion `src/ingest/iem_asos.py`
- Fetch real intraday obs from IEM ASOS, store source-tagged `'iem'` in
  `reference_temp` (the table is already source-tagged: `open_meteo|nws|iem`).
- URL shape (verified format, was 403 only due to allowlist):
  `https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station=<ID>&data=tmpf&year1=&month1=&day1=&year2=&month2=&day2=&tz=Etc/UTC&format=onlycomma&missing=null&latlon=no`
  → CSV with `station,valid,tmpf` (tmpf is °F; `valid` = `YYYY-MM-DD HH:MM` UTC).
- **Station id map** (Polymarket code → IEM id): `KLGA→LGA, KORD→ORD, KMIA→MIA,
  EGLC→EGLC, LFPB→LFPB`. NOTE: **Paris resolves via LFPB (Le Bourget)** in the
  data, not LFPG — verify each event's `resolutionSource` station and use it.
- Dedupe repeated rows on timestamp; use `requests` with backoff (reuse the
  http helper pattern). Persist via `reference_feed.persist_reference(..., source="iem")`.

### 3b. Add unit-aware bucketing to `src/common/normalize.py` (+ tests)
```python
def detect_unit(label):            # "C" if "°c" in label.lower() else "F"
def temp_to_bucket(temp_f, buckets):
    # buckets = [(label, parse_bucket_bounds(label)), ...] in the LABEL's own unit.
    # convert temp_f to the label unit, round to whole degree (resolver reports
    # integer-degree highs), match: range buckets (lo<=r<=hi), 'or below' (r<=hi),
    # 'or above' (r>=lo), single-value °C bucket (r==lo==hi). Return label or None.
```
Add unit tests pinning °F ranges AND °C single-degree (e.g. 68.0°F→"20°C",
69.8°F→"21°C").

### 3c. First decisive gate — real-feed bucket accuracy (hindsight)
Per resolved event: take IEM daily-max, map via `temp_to_bucket`, compare to the
resolved winner. Report per-city + US/intl + overall.
- **If overall accuracy is not clearly > ~70%, the feed itself can't pick the 2°F
  bucket and P1 is dead — stop there and report.**
- (Open-Meteo was ~35%; a real feed should be much higher. This gate decides
  whether to bother with the trading sim.)

### 3d. Causal feed-driven backtest `src/analyze/backtest_p1_feed.py`
- **Causal lock rule** (no peek at the final high): walk the day's obs; once local
  clock ≥ `min_peak_hour_local` AND temp has fallen `decline_margin_f` below the
  running max for `decline_readings` readings → declare the high "in". Fallback:
  end-of-day. The running-max bucket at lock time = the feed's pick.
- Enter that bucket's YES at the **first market print at/after lock_ts** (trade-
  stream execution proxy — state it's optimistic: no queue/slippage/impact). Hold
  to resolution. `won = (feed_bucket == resolved winner)` so early locks that get
  overtaken correctly count as losses.
- **Sweep the lock hour; fit on NYC; judge OUT-OF-SAMPLE.** Report n, feed-hit,
  avg entry, ROI(fee0), ROI(2% fee) per group + per city.
- **The decision:** an edge exists only if `avg_entry < feed_hit` out-of-sample
  AND post-fee OOS ROI is positive and robust across cities/lock-hours. If
  `entry ≈ hit` everywhere, the market already prices what's knowable → no edge →
  say so plainly.

### 3e. Record outcome honestly
Write `reports/backtest_p1_feed.md`, update `reports/spec_p1_resolution_drift.md`
(§1c) and `FINDINGS.md` (§12) with the **real** result. Commit + push. Update
PR #32.

---

## 4. If P1 feed-driven also fails — the remaining honest options

Present these to the operator; don't silently pivot.
1. **P2 maker quoting** — run Phase-8 `clob_live_capture.py` for ~1–2 weeks to
   measure adverse selection (its core risk), THEN backtest quoting. Only real
   path left that isn't a latency race.
2. **Pure latency taker** — be faster than incumbents in the first seconds after
   the temperature/book moves. Infra race, not a signal; needs the fast order
   path. High effort, uncertain.
3. **Different market family** — the recon tooling generalises; weather daily-temp
   may simply be efficiently priced. Other Polymarket markets could be re-scanned.
4. **Stop and write `OVERVIEW.md`** — capture the (valuable, money-saving)
   conclusion that no deployable edge was found, and leave BTC-Bot's existing
   paper-first, validation-gated approach as-is.

The operator wants this to "work as its own bot, not like any before." The honest
framing: a *novel* bot is only worth building on a *real* edge. The recon has
falsified the two obvious ones; 3a–3d is the last cheap test. Be straight about
the odds (~15–20%) before sinking more effort.

---

## 5. If IEM is STILL 403 after the recreate

Then the allowlist change didn't take. Tell the operator exactly:
- The host must be **`mesonet.agron.iastate.edu`** in the environment's network
  policy, and the environment must be **recreated/started fresh** for it to apply
  (editing a running container's policy does not retroactively work).
- Until 200, the feed test cannot run. Do **not** substitute Open-Meteo and call
  it a feed test (it's been shown unfit), and do **not** fabricate results.
- Alternative if they can't allowlist IEM: run the **forward** path —
  `scripts/run_reference_validation.py` daily accumulates NWS-vs-resolver accuracy
  (NWS is reachable but only ~2 days retention), building evidence over real time.

---

## 6. Repo map (key files)

```
polymarket-weather-recon/
  config.yaml            endpoints, windows, keywords, thresholds, backtest params
  FINDINGS.md            verified data-source facts (§0-§11 done; add §12)
  reports/
    weather_markets.csv  data_quality_phase2.md  wallet_classification.csv
    phase4_summary.md    dossiers/ (12 + INDEX + png)  exploits.md
    btc_bot_recommendations.md  insights.json
    backtest_p1.md (OM trigger, dead)  backtest_p1_market.md (price trigger, dead OOS)
    spec_p1_resolution_drift.md  spec_p2_maker_quoting.md  reference_validation.md
  src/
    common/   config.py http.py db.py normalize.py   (ADD temp_to_bucket/detect_unit)
    ingest/   gamma_markets.py data_api_trades.py reference_feed.py
              clob_live_capture.py (Phase 8)  subgraph_trades.py/onchain_fills.py (blocked stubs)
              (ADD iem_asos.py)
    features/ timing sizing pnl reaction wallet_features
    detect/   bot_score clustering operators
    analyze/  profiler exploits backtest_p1 backtest_p1_market reference_validation
              (ADD backtest_p1_feed.py)
    report/   build_report data_quality
  scripts/    run_phase1..8, run_backtest_p1[_market], run_reference_validation, probe_sources
              (ADD run_backtest_p1_feed.py)
  tests/      test_smoke test_normalize test_features   (all pass; keep green)
```

Run order: `run_phase1 → run_phase2 → run_phase3 → run_phase4 → run_phase5 →
run_phase67`; `run_phase8` and the backtests are standalone.

## 7. Git / PR protocol
- Commit + push to `claude/cool-planck-MpcW1` only. `git push -u origin <branch>`,
  retry on network error (2/4/8/16s). PR #32 already open (draft) — update its
  body when the feed result lands. Do not push to other branches.
- Keep `data/raw/`, `data/processed/*.sqlite`, and `reports/wallet_features.csv`
  gitignored (large/regenerable). Dossier `.md`+`.png` and the small CSVs/JSON
  ARE committed deliverables.
