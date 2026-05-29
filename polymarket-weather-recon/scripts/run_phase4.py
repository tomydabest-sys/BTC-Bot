"""Phase 4 driver — bot detection, clustering, operator grouping.

    python scripts/run_phase4.py
Requires Phase 3 (wallet_features table) to have been run.
Outputs: reports/wallet_classification.csv, reports/phase4_summary.md
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.common.config import load_config, project_root  # noqa: E402
from src.common.db import connect  # noqa: E402
from src.detect.bot_score import score_wallets  # noqa: E402
from src.detect.clustering import characterise, cluster_wallets  # noqa: E402
from src.detect.operators import group_operators  # noqa: E402


def main() -> int:
    logging.basicConfig(level="WARNING", format="%(levelname)s %(name)s %(message)s")
    th = load_config()["thresholds"]["bot_score"]
    conn = connect()
    feats = pd.read_sql_query("SELECT * FROM wallet_features", conn)
    trades = pd.read_sql_query(
        "SELECT proxy_wallet, condition_id, timestamp FROM trades", conn)

    scored = score_wallets(feats)
    sdf = scored[scored["scored"]].copy()
    scored_set = set(sdf["proxy_wallet"])

    clustered, cmeta = cluster_wallets(sdf)
    ops = group_operators(trades, scored_set)

    classification = clustered.merge(ops, on="proxy_wallet", how="left")
    keep = ["proxy_wallet", "bot_score", "cluster", "kmeans", "operator_id", "operator_size",
            "c_cadence_regularity", "c_activity_247", "c_size_regularity",
            "c_breadth", "c_burstiness", "c_reaction",
            "n_trades", "active_hours", "cv_gap", "breadth_markets",
            "max_trades_per_sec", "reaction_alignment", "realized_pnl_usdc",
            "volume_usdc", "roi_on_resolved", "fill_win_rate"]
    out = classification[[c for c in keep if c in classification.columns]] \
        .sort_values("bot_score", ascending=False)
    out_path = project_root() / "reports" / "wallet_classification.csv"
    out.to_csv(out_path, index=False)
    out.to_sql("wallet_classification", conn, if_exists="replace", index=False)
    conn.commit()

    # ---- narrative ----
    n_all = len(feats)
    n_scored = len(sdf)
    likely = sdf[sdf["bot_score"] >= 0.5]
    strong = sdf[sdf["bot_score"] >= 0.7]
    # HDBSCAN may find no crisp density clusters (a continuum); profile by the
    # label that actually has structure (prefer KMeans cross-check).
    profile_label = "kmeans" if ("kmeans" in clustered and clustered["kmeans"].nunique() > 1) else "cluster"
    char = characterise(clustered, cmeta.get("features", []), label_col=profile_label)

    lines = ["# Phase 4 — bot detection, clustering & operator grouping", "",
             f"Wallets total: **{n_all}** | scored (≥{th['min_score_trades']} trades): "
             f"**{n_scored}** | bot_score≥0.5: **{len(likely)}** | ≥0.7: **{len(strong)}**", "",
             "## Heuristic score (transparent, weighted; see config weights)",
             "Components: cadence regularity, 24/7 activity, size regularity, breadth, "
             "burstiness, reaction alignment. Wallets <min trades are 'unscored'.", ""]

    lines += ["## Clustering cross-check", f"```\n{cmeta}\n```", "",
              f"HDBSCAN found {cmeta.get('hdbscan_clusters', 0)} crisp density cluster(s) "
              f"({cmeta.get('hdbscan_noise', 0)} noise) — the scored population is largely a "
              "continuum, so the profile below uses the **KMeans** cross-check "
              f"(k={cmeta.get('kmeans_k', '?')}, silhouette {cmeta.get('kmeans_silhouette', '?')}).",
              "",
              f"### Cluster profiles by `{profile_label}` (median features)", "",
              char.round(3).to_markdown(index=False), "",
              "Agreement check: the cluster with high median bot_score should match the "
              "high-scoring heuristic population (raises confidence); per-wallet labels are in "
              "wallet_classification.csv.", ""]

    n_ops = ops["operator_id"].nunique() if not ops.empty else 0
    big = ops[ops["operator_size"] > 30]["operator_id"].nunique() if not ops.empty else 0
    lines += ["## Operator grouping (co-timing; on-chain funding unavailable)",
              f"Multi-wallet operators detected: **{n_ops}** "
              f"(covering {len(ops)} wallets).", ""]
    if big:
        lines += ["> ⚠️ **Over-merge caveat:** transitive union-find (A~B, B~C ⇒ A~C) can "
                  f"fuse a densely co-active bot population into one giant component. "
                  f"{big} operator(s) have >30 wallets and likely represent a co-trading "
                  "*cluster / shared infra*, not a single controller. The small (2–15 "
                  "wallet) components are the more credible same-operator groups.", ""]
    if n_ops:
        top_ops = (ops.groupby("operator_id")["operator_size"].first()
                   .sort_values(ascending=False).head(10))
        lines.append("| operator | wallets |\n|---|--:|")
        for op, sz in top_ops.items():
            lines.append(f"| {op} | {sz} |")
        lines.append("")

    lines += ["## Top 15 by bot_score", "",
              "| wallet | bot_score | n_trades | active_h | breadth | max/s | "
              "react | realized_pnl | roi | win |", "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for _, r in strong.sort_values("bot_score", ascending=False).head(15).iterrows():
        lines.append(
            f"| {r['proxy_wallet'][:10]}… | {r['bot_score']:.2f} | {int(r['n_trades'])} | "
            f"{int(r['active_hours'])} | {int(r['breadth_markets'])} | {int(r['max_trades_per_sec'])} | "
            f"{(r['reaction_alignment'] if pd.notna(r['reaction_alignment']) else float('nan')):.2f} | "
            f"{r['realized_pnl_usdc']:.0f} | "
            f"{(r['roi_on_resolved'] if pd.notna(r['roi_on_resolved']) else float('nan')):.3f} | "
            f"{(r['fill_win_rate'] if pd.notna(r['fill_win_rate']) else float('nan')):.2f} |")
    lines += ["", "## Honesty / confidence",
              "- Taker-side only; maker-fraction & maker adverse-selection absent.",
              "- Reaction alignment is COARSE (hourly reference) — low/medium confidence.",
              "- Operator grouping is co-timing-based (medium confidence; not proof of control).",
              "- Fixed-offset pricing component deferred (no robust historical mid)."]

    md = project_root() / "reports" / "phase4_summary.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"scored={n_scored}  bot>=0.5={len(likely)}  bot>=0.7={len(strong)}  operators={n_ops}")
    print("cluster meta:", cmeta)
    print(f"wrote {out_path}\nwrote {md}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
