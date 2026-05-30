# FINDINGS — verified data-source facts (living doc)

**Phase 0 reconnaissance.** Probed live on **2026-05-29 (UTC)** from the remote
execution sandbox. Every fact below was observed in this session via `curl`/
`requests`, not assumed. Where the brief's notes conflict with observation,
observation wins and the discrepancy is called out.

Re-confirm any time with: `python scripts/probe_sources.py`.

---

## 0. Headline finding — the network is an ALLOWLIST proxy ⚠️

Outbound requests pass through a proxy that returns **HTTP 403 with body
`Host not in allowlist`** for any host not explicitly permitted. This is the
single most important fact for this project: it **removes the brief's two
designated trade sources** and reshapes the plan.

| Host | Result | Role in the brief |
|---|---|---|
| `gamma-api.polymarket.com` | ✅ 200 | market/event metadata |
| `data-api.polymarket.com` | ✅ 200 | trade/activity/positions/holders |
| `clob.polymarket.com` | ✅ 200 | book / price history |
| `api.open-meteo.com` (forecast) | ✅ 200 | weather reference |
| `api.weather.gov` (NWS) | ✅ 200 | weather reference |
| `polymarket.com`, `github.com`, `raw.githubusercontent.com`, `pypi.org` | ✅ 200 | misc / ABIs / installs |
| **`api.goldsky.com`** (subgraph) | ❌ 403 | **brief's PRIMARY trade source** |
| **Polygon RPC** (polygon-rpc, ankr, drpc, publicnode, 1rpc) | ❌ 403 | **brief's FALLBACK/verification (on-chain)** |
| `api.polygonscan.com` | ❌ 403 | on-chain block→time, ABIs |
| `gateway.thegraph.com`, `gateway-arbitrum.network.thegraph.com` | ❌ 403 | alt subgraph |
| **`archive-api.open-meteo.com`** | ❌ 403 | historical weather archive |
| `example.com`, `google.com` (controls) | ❌ 403 | confirm allowlist |

**Consequences**
1. The **Goldsky subgraph (intended PRIMARY) is unavailable.** It is the only
   source giving *both maker and taker*, fees, and log index per fill.
2. **On-chain (intended FALLBACK/verification) is unavailable** — every Polygon
   RPC and Polygonscan are blocked. No completeness cross-check, no `OrderFilled`
   decode, no on-chain funding-source clustering.
3. The **Polymarket Data API becomes the de-facto primary trade source.**
4. Open-Meteo's dedicated **archive** API is blocked, but its **forecast**
   endpoint returns ~90 days of past data (see §5), which is enough for the
   bounded window.

All of this is **reversible by widening the allowlist** (add `api.goldsky.com`
and a Polygon RPC host). The scaffold is built so those ingesters drop in
behind a `reachable:` flag without restructuring.

---

## 1. Gamma API — `gamma-api.polymarket.com` ✅

- `GET /markets?limit=&offset=&closed=` — **limit caps at 100/request**;
  `offset` paginates. `GET /events?...` same cap. Use offset pagination.
- **`tag_slug` market filter is IGNORED** (returned unrelated legacy markets) —
  do weather discovery via `/public-search?q=<kw>` (caps ~50 events/type) **and**
  by paginating `/events` and filtering client-side on the `tags` array +
  keyword pass. `/tags?limit=` is a flat array of `{id,label,slug,...}`.
- **Parsing gotcha:** `clobTokenIds`, `outcomes`, `outcomePrices` are
  **stringified JSON arrays** (e.g. `"[\"id1\",\"id2\"]"`) — `json.loads` each.
- Useful market fields: `conditionId`, `clobTokenIds`, `question`, `slug`,
  `outcomes`, `outcomePrices`, `startDate`, `endDate`/`endDateIso`, `closed`,
  `active`, `volume`, `liquidity`, `resolutionSource`, `resolvedBy`, `negRisk`,
  `umaResolutionStatuses`, `orderPriceMinTickSize` (0.001), `orderMinSize` (5),
  `makerBaseFee`/`takerBaseFee` (observed `1000`; unit unconfirmed — treat fee
  math as unverified, prefer CLOB `/fee-rate` later).
- Useful event fields: `title`, `slug`, `tags[]`, `markets[]`, `negRisk`,
  `resolutionSource`, `volume`, `volume24hr`, `openInterest`, `closed`.

**Weather markets — confirmed shape.** `public-search?q=temperature` returns
events like *"Highest temperature in NYC on April 16?"*. Resolving that event:
- `negRisk: true` — a **multi-bucket neg-risk event** = **11 YES/NO bucket
  markets** (e.g. "77°F or below", "78-79°F", "80-81°F", …), each with its own
  `conditionId` + `clobTokenIds`.
- `resolutionSource = https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA`
  → **station code KLGA (LaGuardia)** is embedded in the URL. Tags:
  `Weather`, `temperature`, `Daily Temperature`, `New York City`, `Recurring`.
- `resolvedBy = 0x69c4…` (UMA adapter), `umaResolutionStatuses` present.

---

## 2. Data API — `data-api.polymarket.com` ✅ (de-facto PRIMARY)

### `/trades?market=<conditionId>&limit=&offset=`
- `market` takes a **conditionId** (verified against a weather bucket).
- **Rows cap at 250/request** (`limit=500` and `limit=1000` both return 250).
- **Pagination terminates cleanly:** offset 0 → 250 rows, offset 250 → 0 rows
  for a ~250-trade market. Rule: page offset by 250 until a page returns 0.
  *Deep offsets beyond the real count return inconsistent 0–1 row artifacts —
  ignore them; the reliable stop signal is an empty page.*
- **One row per fill, single wallet, taker side.** 100 rows ⇒ 100 unique
  `transactionHash` ⇒ exactly one row per tx. Row fields:
  `proxyWallet`, `side` (BUY/SELL), `asset` (token id, decimal string),
  `conditionId`, `size` (**shares**, human decimals), `price` (**[0,1]**),
  `timestamp` (unix s), `outcome`, `outcomeIndex`, `transactionHash`,
  plus profile fields (`name`, `pseudonym`).
- ⚠️ **The maker counterparty is NOT exposed.** This is the key limitation:
  the brief's "model both maker and taker as first-class" is **not satisfiable**
  from the available sources (it needs the blocked subgraph/on-chain). We can
  attribute the **taker-side wallet** of each fill only.

### `/activity?user=<addr>` — like trades, **plus** `usdcSize` and `type`.
### `/positions?user=<addr>` — `avgPrice`, `size`, `initialValue`, `currentValue`,
  `cashPnl`, `realizedPnl`, `percentPnl`, `curPrice`, `redeemable`,
  `negativeRisk`, `oppositeAsset`. **PnL proxy** for ranking (with the caveat
  in `weather_wallet_analysis.md`: open-position snapshot PnL is noisy; trust
  closed round-trips).
### `/holders?market=<conditionId>` — `{token, holders:[{proxyWallet, amount,
  outcomeIndex, …}]}`; current holder snapshot.

---

## 3. CLOB API — `clob.polymarket.com` ✅

- `/` → `"OK"`. `/book?token_id=`, `/midpoint?token_id=`, `/price?token_id=&side=`
  work for **active** markets. Book: `bids/asks[{price,size}]`, `tick_size`,
  `neg_risk`, `last_trade_price`.
- **`/prices-history`:** `interval=1m` → **400 (invalid)**. `interval=max&fidelity=60`
  works for **active** tokens (719 points observed). For **closed** markets,
  `interval=max` returns empty **but explicit `startTs=&endTs=&fidelity=`
  DOES return data** (6 points for an illiquid resolved bucket). Point density
  tracks actual activity, not the requested fidelity.
- Implication: a **coarse historical price/mid series per token is recoverable**
  for closed weather markets — partially restoring "prevailing mid" for Phase-3
  price-signature features and Phase-6 stale-quote analysis, at minute-ish
  resolution and limited density.
- **No order-book history.** Placements/cancellations live off-chain in the
  matching engine and are in NO historical source → maker quote/cancel churn is
  observable ONLY via forward live capture (Phase 8).

---

## 4. Subgraph (Goldsky) & On-chain — BLOCKED ❌

- `api.goldsky.com/...` (orderbook/positions/activity/pnl candidates) →
  403 "Host not in allowlist". `gateway.thegraph.com` likewise.
- All Polygon RPCs + `api.polygonscan.com` → 403.
- **ABIs are still obtainable** (`raw.githubusercontent.com` ✅) for
  documentation/future use, but decoding needs an allowlisted RPC (none).
- Well-known contract addresses are kept in `config.yaml`
  (`sources.onchain_polygon.contracts_unverified`) **flagged UNVERIFIED** — must
  be re-confirmed against Polygonscan/docs before any decode is trusted (per the
  brief: never trust a signature/address from memory).

---

## 5. Weather reference (Phase 3.5) — feasible via proxy ✅ / ❌

- **Open-Meteo forecast** `api.open-meteo.com/v1/forecast` ✅ — supports
  `past_days` up to ~92, returning past hourly/daily series (verified: NYC
  hourly °F, 96h incl. 3 past days). The dedicated **archive** API is **blocked**,
  so **~90 days is the historical reach** via the forecast endpoint.
- **NWS** `api.weather.gov` ✅ — `/stations/KLGA/observations` returns obs in
  **°C** (`wmoUnit:degC`); requires a `User-Agent`. Recent-observation retention
  via the API is limited (not a deep archive).
- **Resolution source is Wunderground** (blocked / scrape-only). The
  authoritative resolved value is Wunderground's station daily high; **Open-Meteo
  / NWS at the SAME station (e.g. KLGA) are a faithful PROXY** of the observable,
  suitable for reaction-latency inference but **not** the exact resolution value.
  Record reference series as proxy, with confidence noted.
- **Measured (Phase backtest/validation):** Open-Meteo's daily max carries a
  **systematic +1.36°F warm bias** vs the station resolver and matches the 2°F
  winning bucket only **~35%** of the time (even with whole-degree rounding) — too
  coarse to drive bucket-level trading. **NWS METAR** (`/stations/<id>/observations`)
  is the actual station class used to resolve and matches far better, BUT the API
  retains only **~2 days** (500-row cap; `limit=1000` → 400). So NWS is a
  **live/forward** reference only; `reference_temp` is source-tagged
  (`open_meteo`|`nws`) and `scripts/run_reference_validation.py` accumulates NWS
  vs resolver accuracy forward (run daily). NWS 5-min METAR is also finer-grained
  than Open-Meteo's hourly (better for reaction latency too).

---

## 6. Units & conventions — VERIFIED ✅

- **Price** ∈ [0,1] USDC/share; observed range [0.001, 0.999]; **min tick 0.001**.
- **`size` = outcome-token shares**, returned as **human decimals** by the Data
  API (e.g. 7.04). **No 1e6 division** for Data-API data.
- **USDC notional = `size * price`**; `/activity.usdcSize` gives it directly.
- On-chain (if ever enabled): USDC 6 dp, outcome token 6 dp — but on-chain is
  currently blocked, so this path is dormant.

---

## 7. Discrepancies vs the brief's notes (observation wins)

1. **"Subgraph is the primary trade source."** → Blocked. Data API is primary.
2. **"On-chain = fallback/verification."** → Blocked. No verification source;
   completeness must be argued from Data-API internal consistency + Gamma volume.
3. **"Model both maker and taker as first-class."** → Not possible from available
   data (taker-side wallet only).
4. **"Resolution source = NWS station/dataset."** → Observed = **Wunderground**
   URL with embedded station code; NWS/Open-Meteo used as proxy.
5. **"Sandbox has no external network"** (old repo handoff) → **outdated**;
   Polymarket + weather APIs ARE reachable now.
6. **Open-Meteo archive** assumed available → blocked; forecast `past_days`
   (~90d) is the workaround.

---

## 8. Weather-market census & first-window sizing

- Weather markets are **abundant**: `public-search` hits its ~50/type cap for
  `temperature`, `rain`, `hurricane`; `wind speed` 35; `snowfall` 2. Daily
  temperature markets recur per city (NYC, London, Paris, Chicago, Miami,
  Seoul, Hong Kong, Amsterdam, Tokyo, Mexico City, …), each a neg-risk event of
  ~11 buckets.
- **Order-of-magnitude estimate** (temperature only): ~10 cities × ~daily ×
  ~11 buckets ⇒ **~3k–10k bucket-markets over ~90 days**; observed ~250
  trades/bucket for an illiquid one (central buckets more) ⇒ roughly
  **10⁴–10⁵ taker-fill rows** for a 90-day temperature backfill. Bounded by the
  250/page Data-API cap, this is very tractable on SQLite.

**Proposed bounded first window** (validate end-to-end before scaling):
- **NYC temperature markets, last 30 days** (single rich KLGA daily series;
  comfortably inside Open-Meteo's ~90-day reach for Phase 3.5).
- Then scale to **5 cities × 60 days** (config `ingestion.scale_up_window`),
  then to all weather metrics / full history (subject to Data-API depth limits).

---

## 9. Storage recommendation

**SQLite as system of record**, with **DuckDB as an optional read-only analytics
layer** over the same file / parquet exports.
- BTC-Bot already uses SQLite (the brief: choose SQLite if BTC-Bot integration
  is the priority — Phase 7 emits SQLite/JSON artifacts it could consume).
- Data volume is moderate (10⁴–10⁵ rows for the first scaled window), well within
  SQLite's comfort zone; no need for a columnar store as the primary.
- DuckDB can `read`/attach the SQLite DB and parquet directly for fast feature
  scans/clustering in Phases 3–4 without duplicating the source of truth.

---

## 10. Phase 1 + 2 execution — bounded window (NYC temperature, 30 days)

Ran 2026-05-29 against the live Data API. Window: NYC temperature, last 30 days.

**Phase 1 discovery:** 29 daily-temperature events found via deterministic
date-slug enumeration → **319 YES/NO bucket-markets**, all with station `KLGA`
parsed from the Wunderground resolution URL, 0 flagged for review. Output:
`reports/weather_markets.csv` + `markets` table.

**Phase 2 ingestion:** **159,920 trades** across all 319 markets (0 zero-trade),
**9,095 unique taker wallets**, **$2.39M** taker-side USDC, prices in
[0.001, 0.999], date range 2026-04-27 → 2026-05-29. Output: `trades` table +
`reports/data_quality_phase2.md`.

**Pagination depth — CORRECTION to the Phase-0 worry.** Real markets paginate
cleanly far past 250: per-market trades ranged **4 → 2,152** (median 372),
pages_fetched up to **9**, and **0 markets landed on an exact 250-multiple**, so
**no truncation was observed** in this window. The Phase-0 "deep offset returns
1 row" behaviour was specific to one near-empty illiquid bucket, not a general
cap. Pagination terminates reliably on a partial/empty page.

**Dedupe/idempotency verified:** distinct `trade_uid` == total rows (159,920);
re-running skips completed markets via `ingest_log`.

**Completeness cross-check:** for the May-6 NYC event, ingested taker-side USDC
($333k) was **0.76×** Gamma's reported event volume ($439k) — a sane band
(Gamma counts roughly both legs), indicating no gross trade loss. This is the
best completeness signal available without the (blocked) subgraph/on-chain.

---

## 11. Option-2 (weather-feed P1) — blockers found while testing

Attempt to properly backtest the weather-feed-driven resolution-drift version hit
two hard walls:

**(a) No usable historical station feed for the trade window.**
- NWS (`api.weather.gov`): reachable but ~2-day retention → live only.
- NCEI (`ncei.noaa.gov`, ISD global-hourly): reachable but **archive lag of
  months** — June 2025 returns data; March/April/May 2026 are all empty. Does NOT
  cover our Apr–May 2026 markets.
- IEM ASOS (`mesonet.agron.iastate.edu`), aviationweather.gov, Synoptic, Meteostat:
  all **403 (blocked by allowlist)** — these are the low-latency archives that
  *would* cover the window.
- ⇒ The proper historical backtest of the weather version is **not possible**
  without allowlisting a low-latency archive (recommended: `mesonet.agron.iastate.edu`),
  or several weeks of forward NWS+price capture.

**(b) Open-Meteo (the only reachable historical feed) is unfit as the resolver proxy.**
Bucket-ID accuracy (does the daily-max land in the resolved bucket?), US cities:
Miami 61% (bias ≈0), NYC 35%→59% bias-corrected (bias +1.36°F), Chicago 39% (bias
**+6.48°F**). Bias varies wildly by city and does NOT transfer (NYC-fit correction
gives Chicago/Miami only 33–39%). Best case ~60% — a *modeled* feed can't reliably
pick a 2°F bucket.

**(c) ⚠️ Units defect for non-US markets.** London & Paris temperature markets are
denominated in **°C with single-degree buckets** (`9°C`, `11°C`), not °F ranges.
The pipeline assumed °F everywhere (`parse_bucket_bounds` strips only `°F`; reference
fetched in °F), so **all temperature-derived numbers for London/Paris are invalid**
(reaction features, reference validation). US cities (NYC/Chicago/Miami) are °F and
correct. **Price-based analyses are unaffected** (they never use temperature), so the
out-of-sample price-backtest failure verdict stands. Fixing this needs per-market
unit detection (°C/°F) + single-degree-bucket handling before any multi-region
weather-signal work.

**Strategic note:** the price-triggered version already failed out-of-sample,
implying the price-lag window a weather feed would exploit is small/efficient
across cities. Combined with (a)–(c), the realistic odds that the weather-feed
version yields a deployable edge are low (~15–20%).
