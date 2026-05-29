"""Phase 4 — unsupervised wallet clustering.

Standardise a feature subset (config: thresholds.clustering.features), run
HDBSCAN (density; label -1 = noise) and KMeans+silhouette as a cross-check, and
characterise clusters. Only SCORED wallets (enough trades) are clustered.
Agreement between clusters and the heuristic bot score raises confidence.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.cluster import HDBSCAN, KMeans
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import StandardScaler

from ..common.config import load_config


def cluster_wallets(feats_scored: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    cfg = load_config()["thresholds"]["clustering"]
    cols = [c for c in cfg["features"] if c in feats_scored.columns]
    df = feats_scored.copy()
    X = df[cols].replace([np.inf, -np.inf], np.nan).fillna(df[cols].median()).to_numpy()
    if len(df) < cfg["min_cluster_size"] * 2:
        df["cluster"] = -1
        return df, {"note": "too few scored wallets to cluster", "n": len(df)}
    Xs = StandardScaler().fit_transform(X)

    hdb = HDBSCAN(min_cluster_size=cfg["min_cluster_size"], copy=True)
    df["cluster"] = hdb.fit_predict(Xs)

    meta: dict = {"features": cols, "n_scored": len(df),
                  "hdbscan_clusters": int(len({c for c in df["cluster"] if c != -1})),
                  "hdbscan_noise": int((df["cluster"] == -1).sum())}

    # KMeans cross-check: pick k by silhouette over a small range
    best_k, best_s = None, -1.0
    for k in range(2, min(8, len(df) - 1)):
        labels = KMeans(n_clusters=k, n_init=10, random_state=0).fit_predict(Xs)
        if len(set(labels)) > 1:
            s = silhouette_score(Xs, labels)
            if s > best_s:
                best_k, best_s = k, s
    if best_k:
        df["kmeans"] = KMeans(n_clusters=best_k, n_init=10, random_state=0).fit_predict(Xs)
        meta["kmeans_k"] = best_k
        meta["kmeans_silhouette"] = round(float(best_s), 3)
    return df, meta


def characterise(df: pd.DataFrame, cols: list[str], label_col: str = "cluster") -> pd.DataFrame:
    """Median feature profile + bot-score per cluster label."""
    agg = {c: "median" for c in cols if c in df.columns}
    agg["bot_score"] = "median"
    out = df.groupby(label_col).agg(agg)
    out["n_wallets"] = df.groupby(label_col).size()
    return out.reset_index()
