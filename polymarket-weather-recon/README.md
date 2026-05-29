# polymarket-weather-recon

Reconnaissance + analysis of **Polymarket weather-market trading**: ingest trades,
attribute them to wallets, detect bots, reverse-engineer top-bot strategies, and
surface exploitable patterns. Findings feed *proposals* (Phase 7) for the
separate BTC-Bot project.

> **Read-only w.r.t. BTC-Bot.** This project never imports from, writes to, or
> calls BTC-Bot's runtime. It lives in its own directory; recommendations are
> markdown/JSON for review only.

## Status: Phases 0–7 complete (bounded window) — awaiting choice of proposals

Done on the bounded NYC-temperature/30-day window:
- **P0** recon+scaffold · **P1** discovery (319 markets) · **P2** ingestion
  (159,920 trades, 9,095 wallets, $2.39M taker-side).
- **P3/3.5** per-wallet features (40 cols) + Open-Meteo KLGA reference & coarse
  reaction alignment.
- **P4** bot score + HDBSCAN/KMeans + co-timing operators: **748 scored**,
  **191 bot_score≥0.5**, **17≥0.7**, **10 operators**.
- **P5** 12 top-bot/operator dossiers (archetype + confidence + plots).
- **P6** exploits: resolution-drift (≈$0.146/share convergence left on $1.32M
  resolution-day notional), thin-hour holes, tail-bucket mispricing.
- **P7** BTC-Bot proposals (P1–P5) + machine-readable `reports/insights.json`.

Outputs in `reports/`: `weather_markets.csv`, `data_quality_phase2.md`,
`wallet_classification.csv`, `phase4_summary.md`, `dossiers/`, `exploits.md`,
`btc_bot_recommendations.md`, `insights.json`. FINDINGS.md §10.
Run order: `run_phase1 → run_phase2 → run_phase3 → run_phase4 → run_phase5 → run_phase67`.
**Next:** pick which proposals (P1–P5) to spec; optionally widen the allowlist /
scale the sample. Nothing is implemented in BTC-Bot.

Start here:
- **`FINDINGS.md`** — verified facts about every data source (the important read).
- **`config.yaml`** — all endpoints, addresses, windows, keywords, thresholds.
- **`scripts/probe_sources.py`** — re-runnable source verification.

### Phase 0 headline
The sandbox network is an **allowlist proxy**. Reachable: Gamma, Data API, CLOB,
Open-Meteo (forecast), NWS, GitHub, PyPI. **Blocked:** the Goldsky **subgraph**
(brief's intended *primary* trade source), **all Polygon RPC + Polygonscan**
(intended *fallback/verification*), and Open-Meteo's *archive* API. Net effect:
the **Data API is the de-facto primary trade source**, maker↔taker pairing is
**not recoverable** (taker-side only), and historical weather comes from
Open-Meteo `past_days` (~90 days). All reversible by widening the allowlist.

## Layout
```
config.yaml      FINDINGS.md      README.md      requirements.txt
scripts/probe_sources.py          # re-runnable Phase 0 recon
src/common/      config + caching/backoff HTTP client
src/ingest/      gamma_markets, data_api_trades (primary), subgraph_trades &
                 onchain_fills (blocked stubs), reference_feed, clob_live_capture
src/features/    timing, sizing, pnl, reaction, wallet_features   (Phase 3/3.5)
src/detect/      bot_score, clustering, operators                 (Phase 4)
src/analyze/     profiler, exploits                               (Phase 5/6)
src/report/      build_report                                     (Phase 7)
data/raw/        cached source responses (gitignored)
data/processed/  normalised SQLite (gitignored)
reports/         generated dossiers / exploits / recommendations
tests/           smoke tests (offline-safe)
```

## Run
```bash
pip install -r requirements.txt          # only requests + PyYAML for Phase 0
python scripts/probe_sources.py          # verify the live data landscape
python -m pytest -q                      # offline smoke tests
```

## Phases & checkpoints
Phases 1–8 per the brief. **Stop and summarise after Phase 0, Phase 2, Phase 4,
and before the full-history backfill.** Phase-1+ modules are stubs until each
checkpoint is signed off.
