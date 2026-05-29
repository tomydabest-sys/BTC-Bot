"""Phase 2 — PRIMARY trade ingestion via the Polymarket Data API.

Under the current network allowlist the Goldsky subgraph and Polygon RPC are
BLOCKED, so the Data API is the primary trade source. See FINDINGS.md §2 for the
verified shape/limits this relies on:
  * /trades?market=<conditionId>&limit=250&offset=N  (250/page cap)
  * paginate offset by 250 until a page returns 0 rows
  * one row per fill, TAKER side only (maker not exposed)
  * size = shares, price in [0,1], usdc = size*price

Idempotent + resumable: each trade gets a deterministic uid (INSERT OR IGNORE),
and per-market completion is recorded in ingest_log so re-runs skip done markets.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..common import normalize as N
from ..common.config import load_config
from ..common.db import connect, init_db
from ..common.http import get_json

log = logging.getLogger("recon.trades")
PAGE = 250


def _fetch_market_trades(condition_id: str, base_url: str, cache_bust: bool) -> tuple[list[dict], int]:
    """Return all trade rows for a market + pages fetched. Stops on empty page."""
    rows: list[dict] = []
    pages = 0
    offset = 0
    while True:
        page = get_json(f"{base_url}/trades", source="data_api_trades",
                        params={"market": condition_id, "limit": PAGE, "offset": offset},
                        cache_bust=cache_bust)
        pages += 1
        if not isinstance(page, list) or len(page) == 0:
            break
        rows.extend(page)
        if len(page) < PAGE:        # last partial page -> done
            break
        offset += PAGE
        if offset > 50_000:         # safety guard against pathological loops
            log.warning("offset guard hit for %s", condition_id)
            break
    return rows, pages


def _normalise(raw: dict, city: str | None) -> dict:
    size = float(raw.get("size") or 0)
    price = float(raw.get("price") or 0)
    ts = int(raw.get("timestamp") or 0)
    return {
        "trade_uid": N.trade_uid(raw),
        "condition_id": raw.get("conditionId"),
        "asset": raw.get("asset"),
        "proxy_wallet": raw.get("proxyWallet"),
        "side": raw.get("side"),
        "side_attribution": "taker_only",
        "size": size,
        "price": price,
        "usdc": N.usdc_notional(size, price),
        "outcome": raw.get("outcome"),
        "outcome_index": raw.get("outcomeIndex"),
        "timestamp": ts,
        "ts_iso": N.iso_from_unix(ts) if ts else None,
        "transaction_hash": raw.get("transactionHash"),
        "city": city,
        "question": raw.get("title"),
    }


def ingest_market_trades(condition_id: str, city: str | None = None,
                         conn=None, cache_bust: bool = False) -> dict:
    cfg = load_config()
    base_url = cfg["sources"]["data_api"]["base_url"]
    own_conn = conn is None
    conn = conn or connect()
    if own_conn:
        init_db(conn)

    raw_rows, pages = _fetch_market_trades(condition_id, base_url, cache_bust)
    ingested_at = datetime.now(timezone.utc).isoformat()
    inserted = 0
    with conn:
        for raw in raw_rows:
            r = _normalise(raw, city)
            cur = conn.execute(
                """INSERT OR IGNORE INTO trades
                   (trade_uid,condition_id,asset,proxy_wallet,side,side_attribution,
                    size,price,usdc,outcome,outcome_index,timestamp,ts_iso,
                    transaction_hash,city,question,ingested_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["trade_uid"], r["condition_id"], r["asset"], r["proxy_wallet"],
                 r["side"], r["side_attribution"], r["size"], r["price"], r["usdc"],
                 r["outcome"], r["outcome_index"], r["timestamp"], r["ts_iso"],
                 r["transaction_hash"], r["city"], r["question"], ingested_at),
            )
            inserted += cur.rowcount
        conn.execute(
            """INSERT OR REPLACE INTO ingest_log
               (condition_id,rows_ingested,pages_fetched,complete,fetched_at)
               VALUES (?,?,?,1,?)""",
            (condition_id, len(raw_rows), pages, ingested_at),
        )
    if own_conn:
        conn.close()
    return {"condition_id": condition_id, "raw": len(raw_rows),
            "inserted": inserted, "pages": pages}


def ingest_discovered_markets(cache_bust: bool = False, skip_done: bool = True) -> dict:
    """Ingest trades for every market in the `markets` table (Phase 1 output)."""
    conn = connect()
    init_db(conn)
    markets = conn.execute(
        "SELECT condition_id, city FROM markets WHERE condition_id IS NOT NULL").fetchall()
    done = set()
    if skip_done and not cache_bust:
        done = {r["condition_id"] for r in conn.execute(
            "SELECT condition_id FROM ingest_log WHERE complete=1").fetchall()}

    totals = {"markets": 0, "skipped": 0, "raw": 0, "inserted": 0}
    for m in markets:
        cid = m["condition_id"]
        if cid in done:
            totals["skipped"] += 1
            continue
        res = ingest_market_trades(cid, city=m["city"], conn=conn, cache_bust=cache_bust)
        totals["markets"] += 1
        totals["raw"] += res["raw"]
        totals["inserted"] += res["inserted"]
        log.info("ingested %s: raw=%d inserted=%d pages=%d",
                 cid[:12], res["raw"], res["inserted"], res["pages"])
    conn.close()
    return totals
