# polymarket-weather-recon

Reconnaissance + analysis of **Polymarket weather-market trading**: ingest trades,
attribute them to wallets, detect bots, reverse-engineer top-bot strategies, and
surface exploitable patterns. Findings feed *proposals* (Phase 7) for the
separate BTC-Bot project.

> **Read-only w.r.t. BTC-Bot.** This project never imports from, writes to, or
> calls BTC-Bot's runtime. It lives in its own directory; recommendations are
> markdown/JSON for review only.

## Status: Phases 0–2 complete (bounded window) — awaiting Phase 2 checkpoint sign-off

Phase 0 (recon+scaffold), Phase 1 (weather-market discovery), and Phase 2
(trade ingestion for the bounded NYC-temperature/30-day window) are done:
**319 markets → 159,920 trades, 9,095 wallets, $2.39M** taker-side volume. See
`reports/weather_markets.csv`, `reports/data_quality_phase2.md`, FINDINGS.md §10.
Next stop is the Phase 2 checkpoint (confirm before scaling to full history).

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
