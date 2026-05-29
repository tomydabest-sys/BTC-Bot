"""Phase 4 — operator grouping (behavioural co-timing).

Funding-source linkage needs on-chain data (BLOCKED), so operators are inferred
purely from CO-MOVEMENT: wallets that repeatedly trade the SAME market within a
few seconds of each other across MANY markets are likely one operator running
several wallets. Restricted to scored (bot-like) wallets to keep it meaningful.

Method: bucket trades by (market, floor(ts/window)); within a bucket every wallet
pair "co-traded". Count distinct markets per pair; pairs co-trading >=
min_cotrade_markets markets become graph edges; connected components = operators.
Confidence: MEDIUM — co-timing is suggestive, not proof of common control.
"""
from __future__ import annotations

import itertools
import logging
from collections import defaultdict

import pandas as pd

from ..common.config import load_config

log = logging.getLogger("recon.operators")
_MAX_WALLETS_PER_BUCKET = 15  # skip busy moments (not operator signal)


class _UF:
    def __init__(self):
        self.p: dict = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def group_operators(trades: pd.DataFrame, scored_wallets: set) -> pd.DataFrame:
    cfg = load_config()["thresholds"]["operators"]
    window = cfg["cotrade_window_s"]
    min_markets = cfg["min_cotrade_markets"]

    df = trades[trades["proxy_wallet"].isin(scored_wallets)].copy()
    df["bucket"] = (df["timestamp"] // window).astype("int64")

    pair_markets: dict[tuple, set] = defaultdict(set)
    grp = df.groupby(["condition_id", "bucket"])["proxy_wallet"]
    for (cid, _), wallets in grp:
        uniq = sorted(set(wallets))
        if len(uniq) < 2 or len(uniq) > _MAX_WALLETS_PER_BUCKET:
            continue
        for a, b in itertools.combinations(uniq, 2):
            pair_markets[(a, b)].add(cid)

    uf = _UF()
    edges = 0
    for (a, b), markets in pair_markets.items():
        if len(markets) >= min_markets:
            uf.union(a, b)
            edges += 1

    comp: dict = defaultdict(list)
    for w in scored_wallets:
        comp[uf.find(w)].append(w)

    rows = []
    op_id = 0
    for members in comp.values():
        if len(members) < 2:
            continue
        op_id += 1
        for w in members:
            rows.append({"proxy_wallet": w, "operator_id": f"op_{op_id:03d}",
                         "operator_size": len(members)})
    log.info("operators: %d edges, %d multi-wallet operators", edges, op_id)
    return pd.DataFrame(rows, columns=["proxy_wallet", "operator_id", "operator_size"])
