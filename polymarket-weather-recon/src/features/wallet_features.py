"""Phase 3 — assemble per-wallet feature vectors.

Loads the normalised trades + markets + reference from SQLite, computes the
timing / sizing / pnl / reaction / cross-market sub-features, merges them per
wallet, and persists a `wallet_features` table + reports/wallet_features.csv.

Honesty: every feature is derived from TAKER-SIDE fills only (maker counterparty
unavailable). Maker-fraction / maker-adverse-selection features are therefore
absent, not estimated.
"""
from __future__ import annotations

import logging

import pandas as pd

from ..common.config import load_config, project_root
from ..common.db import connect
from . import pnl as pnl_mod
from . import reaction as reaction_mod
from . import sizing as sizing_mod
from . import timing as timing_mod

log = logging.getLogger("recon.features")


def _load(conn):
    trades = pd.read_sql_query(
        "SELECT proxy_wallet, condition_id, asset, side, size, price, usdc, "
        "outcome_index, timestamp FROM trades", conn)
    markets = pd.read_sql_query(
        "SELECT condition_id, event_slug, bucket_label, station, end_date, "
        "resolved, winning_outcome_index FROM markets", conn)
    try:
        reference = pd.read_sql_query("SELECT station, ts, temp_f FROM reference_temp", conn)
    except Exception:
        reference = pd.DataFrame(columns=["station", "ts", "temp_f"])
    return trades, markets, reference


def cross_market_features(trades: pd.DataFrame, markets: pd.DataFrame) -> pd.DataFrame:
    ev = markets.set_index("condition_id")[["event_slug", "end_date"]]
    df = trades.join(ev, on="condition_id")
    df["target_date"] = df["end_date"].astype(str).str[:10]
    rows = []
    for wallet, g in df.groupby("proxy_wallet"):
        per_sec = g.groupby("timestamp")["condition_id"].nunique()
        multi = g.groupby("timestamp")["condition_id"].transform("nunique") > 1
        rows.append({
            "proxy_wallet": wallet,
            "breadth_markets": int(g["condition_id"].nunique()),
            "breadth_events": int(g["event_slug"].nunique()),
            "breadth_days": int(g["target_date"].nunique()),
            "max_markets_same_second": int(per_sec.max()) if len(per_sec) else 1,
            "multi_market_second_frac": float(multi.mean()),
        })
    return pd.DataFrame(rows)


def build_wallet_features(min_trades: int = 1) -> pd.DataFrame:
    cfg = load_config()
    tz_map = {k: v.get("tz", "UTC") for k, v in
              cfg["weather_reference"]["stations"].items()}
    conn = connect()
    trades, markets, reference = _load(conn)
    log.info("loaded %d trades, %d markets, %d reference points",
             len(trades), len(markets), len(reference))

    feats = timing_mod.timing_features(trades)
    for part in (sizing_mod.sizing_features(trades),
                 pnl_mod.settlement_pnl(trades, markets),
                 reaction_mod.reaction_features(trades, markets, reference, tz_map),
                 cross_market_features(trades, markets)):
        feats = feats.merge(part, on="proxy_wallet", how="left")

    first_last = trades.groupby("proxy_wallet")["timestamp"].agg(
        first_activity_ts="min", last_activity_ts="max").reset_index()
    feats = feats.merge(first_last, on="proxy_wallet", how="left")
    feats["lifespan_days"] = (feats["last_activity_ts"] - feats["first_activity_ts"]) / 86400.0

    feats = feats[feats["n_trades"] >= min_trades].reset_index(drop=True)
    _persist(conn, feats)
    conn.close()

    out = project_root() / "reports" / "wallet_features.csv"
    feats.sort_values("n_trades", ascending=False).to_csv(out, index=False)
    log.info("wrote %d wallet feature rows -> %s", len(feats), out)
    return feats


def _persist(conn, feats: pd.DataFrame) -> None:
    feats.to_sql("wallet_features", conn, if_exists="replace", index=False)
    conn.commit()
