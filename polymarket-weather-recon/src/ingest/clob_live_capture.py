"""Phase 8 — forward live-book capture (OPTIONAL, confirmed).

Standalone long-running collector. Snapshots the CLOB order book for active
weather-market buckets at a fixed cadence and persists top-of-book + depth over
time into the same `processed/` SQLite. This is the ONLY way to observe maker
quote/cancel churn and rebate-harvesting behaviour — historical sources cannot
recover it (placements/cancels are off-chain in the matching engine).

Design notes:
  * live fetches MUST bypass the on-disk cache (each snapshot is new data) -> we
    call get_json(..., cache=False).
  * best bid/ask are computed via max/min over levels (do NOT trust array order).
  * book summary row is written every cycle (a time series, including unchanged
    books); full levels are written only when the book hash changes (saves space
    while preserving the churn record).
  * idempotent w.r.t. crashes: each cycle is independent; restart resumes.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from ..common import normalize as N
from ..common.config import load_config
from ..common.db import connect, init_db
from ..common.http import get_json

log = logging.getLogger("recon.clob_capture")


# --------------------------------------------------------------------------- #
# discovery of currently-active weather tokens
# --------------------------------------------------------------------------- #
def discover_active_weather_tokens(cache_bust: bool = True) -> list[dict]:
    cfg = load_config()
    base = cfg["sources"]["gamma"]["base_url"]
    disc = cfg["weather_discovery"]
    p8 = cfg["phase8_live_capture"]
    template, city_slugs = disc["event_slug_template"], disc["city_slugs"]
    today = datetime.now(timezone.utc).date()
    dates = [today + timedelta(days=i) for i in range(0, p8["forward_days"] + 1)]

    tokens: list[dict] = []
    for city in p8["cities"]:
        cslug = city_slugs.get(city)
        if not cslug:
            continue
        for d in dates:
            slug = N.build_event_slug(template, cslug, d)
            ev = get_json(f"{base}/events", source="gamma",
                          params={"slug": slug}, cache=False if cache_bust else True)
            if not ev:
                continue
            event = ev[0]
            res = event.get("resolutionSource")
            for m in event.get("markets", []) or []:
                if m.get("closed"):
                    continue
                tids = N.parse_json_array(m.get("clobTokenIds"))
                outs = N.parse_json_array(m.get("outcomes")) or ["Yes", "No"]
                pairs = list(zip(tids, outs))
                if not p8.get("capture_both_outcomes", False):
                    pairs = pairs[:1]   # YES only
                for tid, outcome in pairs:
                    tokens.append({
                        "token_id": tid, "condition_id": m.get("conditionId"),
                        "city": city, "bucket_label": m.get("groupItemTitle") or m.get("question"),
                        "outcome": outcome, "end_date": m.get("endDate") or event.get("endDate"),
                        "station": N.station_from_resolution(m.get("resolutionSource") or res),
                    })
    return tokens[: p8["max_tokens"]]


# --------------------------------------------------------------------------- #
# one snapshot of one token's book
# --------------------------------------------------------------------------- #
def _summarise_book(book: dict) -> dict:
    def lv(side):
        return [(float(x["price"]), float(x["size"])) for x in (book.get(side) or [])]
    bids, asks = lv("bids"), lv("asks")
    best_bid = max((p for p, _ in bids), default=None)
    best_ask = min((p for p, _ in asks), default=None)
    bb_size = next((s for p, s in bids if p == best_bid), None) if best_bid is not None else None
    ba_size = next((s for p, s in asks if p == best_ask), None) if best_ask is not None else None
    mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None
    return {
        "best_bid": best_bid, "best_bid_size": bb_size,
        "best_ask": best_ask, "best_ask_size": ba_size,
        "mid": mid, "spread": spread,
        "n_bids": len(bids), "n_asks": len(asks),
        "bid_depth_usdc": round(sum(p * s for p, s in bids), 4),
        "ask_depth_usdc": round(sum(p * s for p, s in asks), 4),
        "last_trade_price": float(book["last_trade_price"]) if book.get("last_trade_price") else None,
        "tick_size": float(book["tick_size"]) if book.get("tick_size") else None,
        "book_hash": book.get("hash"),
        "server_ts": int(book["timestamp"]) if book.get("timestamp") else None,
        "_bids": sorted(bids, key=lambda x: -x[0]),
        "_asks": sorted(asks, key=lambda x: x[0]),
    }


def snapshot_once(tokens: list[dict], conn, capture_depth: bool,
                  depth_levels: int, last_hash: dict) -> int:
    cfg = load_config()
    base = cfg["sources"]["clob"]["base_url"]
    capture_ts = int(time.time())
    written = 0
    with conn:
        for tk in tokens:
            try:
                book = get_json(f"{base}/book", source="clob_live",
                                params={"token_id": tk["token_id"]}, cache=False)
            except Exception as exc:
                log.warning("book fetch failed for %s: %s", tk["token_id"][:12], exc)
                continue
            s = _summarise_book(book)
            cur = conn.execute(
                """INSERT INTO book_snapshots
                   (token_id,condition_id,city,bucket_label,outcome,capture_ts,server_ts,
                    best_bid,best_bid_size,best_ask,best_ask_size,mid,spread,
                    n_bids,n_asks,bid_depth_usdc,ask_depth_usdc,last_trade_price,
                    tick_size,book_hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tk["token_id"], tk["condition_id"], tk["city"], tk["bucket_label"],
                 tk["outcome"], capture_ts, s["server_ts"], s["best_bid"], s["best_bid_size"],
                 s["best_ask"], s["best_ask_size"], s["mid"], s["spread"], s["n_bids"],
                 s["n_asks"], s["bid_depth_usdc"], s["ask_depth_usdc"], s["last_trade_price"],
                 s["tick_size"], s["book_hash"]))
            written += 1
            # full levels only when the book changed (saves space, keeps churn record)
            if capture_depth and s["book_hash"] != last_hash.get(tk["token_id"]):
                sid = cur.lastrowid
                rows = []
                for i, (p, sz) in enumerate(s["_bids"][:depth_levels]):
                    rows.append((sid, "bid", i, p, sz))
                for i, (p, sz) in enumerate(s["_asks"][:depth_levels]):
                    rows.append((sid, "ask", i, p, sz))
                conn.executemany(
                    "INSERT INTO book_levels (snapshot_id,side,level,price,size) VALUES (?,?,?,?,?)",
                    rows)
                last_hash[tk["token_id"]] = s["book_hash"]
    return written


# --------------------------------------------------------------------------- #
# long-running loop
# --------------------------------------------------------------------------- #
def run_collector(max_cycles: int | None = None, duration_s: int | None = None,
                  cadence_s: int | None = None, refresh_tokens_every: int = 40) -> dict:
    cfg = load_config()["phase8_live_capture"]
    cadence = cadence_s or cfg["cadence_seconds"]
    conn = connect()
    init_db(conn)
    tokens = discover_active_weather_tokens()
    log.info("live capture: %d active tokens, cadence %ds", len(tokens), cadence)
    if not tokens:
        log.warning("no active weather tokens found — nothing to capture")
        conn.close()
        return {"cycles": 0, "rows": 0, "tokens": 0}

    last_hash: dict = {}
    start = time.time()
    cycles = rows = 0
    while True:
        rows += snapshot_once(tokens, conn, cfg["capture_depth"], cfg["depth_levels"], last_hash)
        cycles += 1
        log.info("cycle %d: %d snapshot rows (total %d)", cycles, len(tokens), rows)
        if max_cycles and cycles >= max_cycles:
            break
        if duration_s and (time.time() - start) >= duration_s:
            break
        if cycles % refresh_tokens_every == 0:        # re-discover (events roll over)
            tokens = discover_active_weather_tokens() or tokens
        time.sleep(cadence)
    conn.close()
    return {"cycles": cycles, "rows": rows, "tokens": len(tokens)}
