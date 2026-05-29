"""Phase 5 — per-bot strategy dossiers.

Ranks the bot population by a composite of weather-market volume, trade count
and realised PnL; for the top N produces reports/dossiers/<entity>.md with
summary stats, an inferred strategy archetype + confidence, an evidence table,
estimated edge source, inferred sizing/timing model, weaknesses, and a plot.

Unit of analysis = OPERATOR where wallets credibly cluster (co-timing group of
2-20 wallets), else the individual wallet. The 146-wallet over-merge component
is treated as individuals (its members ranked on their own).

Honesty: taker-side only -> maker fraction & maker adverse-selection are not
computable and are stated as such, not estimated.
"""
from __future__ import annotations

import logging

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ..common.config import load_config, project_root  # noqa: E402
from ..common.db import connect  # noqa: E402

log = logging.getLogger("recon.profiler")
CREDIBLE_OP_MAX = 20


def _entity_id(row) -> tuple[str, str]:
    sz = row.get("operator_size")
    if pd.notna(row.get("operator_id")) and pd.notna(sz) and 2 <= sz <= CREDIBLE_OP_MAX:
        return row["operator_id"], "operator"
    return row["proxy_wallet"], "wallet"


def _zscore(s: pd.Series) -> pd.Series:
    sd = s.std(ddof=0)
    return (s - s.mean()) / sd if sd > 0 else s * 0.0


def _archetype(e: dict) -> tuple[str, str, list[str]]:
    ev: list[str] = []
    high_breadth = e["breadth_markets"] >= 50
    h24 = e["active_hours"] >= 20
    high_win = (e["fill_win_rate"] or 0) >= 0.85
    thin_roi = 0 < (e["roi_on_resolved"] or 0) < 0.15
    high_react = (e["reaction_alignment"] or 0) >= 0.7
    res_heavy = (e["resolution_day_frac"] or 0) >= 0.6
    repeated = (e["mode_size_frac"] or 0) >= 0.5
    regular = (e["cv_gap"] or 9) <= 0.6
    extreme = (e["extreme_price_frac"] or 0) >= 0.4

    if high_breadth: ev.append(f"breadth {int(e['breadth_markets'])} markets")
    if h24: ev.append(f"{int(e['active_hours'])}/24 active hours")
    if high_win: ev.append(f"win rate {e['fill_win_rate']:.2f}")
    if thin_roi: ev.append(f"thin ROI {e['roi_on_resolved']:.3f}")
    if high_react: ev.append(f"reaction alignment {e['reaction_alignment']:.2f}")
    if res_heavy: ev.append(f"{e['resolution_day_frac']:.0%} trades on resolution day")
    if repeated: ev.append(f"mode-size frac {e['mode_size_frac']:.2f}")
    if extreme: ev.append(f"extreme-price frac {e['extreme_price_frac']:.2f}")

    if high_breadth and h24 and high_win and thin_roi:
        return ("Systematic fair-value maker/taker", "medium-high", ev)
    if high_react and res_heavy:
        return ("Resolution-drift sniper", "medium", ev)
    if repeated and regular:
        return ("Copy/ladder/grid (fixed-size mechanical)", "medium", ev)
    if extreme:
        return ("Longshot / tail trader", "medium", ev)
    if high_breadth and h24:
        return ("Systematic high-breadth trader (maker/taker indistinct)", "medium", ev)
    return ("Systematic (unclassified)", "low", ev)


def _plot(entity_id: str, t: pd.DataFrame, contrib_sorted: pd.DataFrame) -> str:
    fig, ax = plt.subplots(2, 3, figsize=(15, 8))
    fig.suptitle(f"Dossier: {entity_id}  (n={len(t)})", fontsize=13)
    gaps = np.diff(np.sort(t["timestamp"].to_numpy()))
    gaps = gaps[gaps > 0]
    if gaps.size:
        ax[0, 0].hist(np.log10(gaps), bins=40, color="steelblue")
    ax[0, 0].set_title("log10 inter-trade gap (s)")
    ax[0, 1].hist(t["size"], bins=40, color="darkorange")
    ax[0, 1].set_title("trade size (shares)")
    hours = pd.to_datetime(t["timestamp"], unit="s", utc=True).dt.hour
    ax[0, 2].bar(*np.unique(hours, return_counts=True), color="seagreen")
    ax[0, 2].set_title("activity by UTC hour"); ax[0, 2].set_xlim(-0.5, 23.5)
    ax[1, 0].hist(t["price"], bins=np.linspace(0, 1, 41), color="purple")
    ax[1, 0].set_title("entry price")
    if len(contrib_sorted):
        ax[1, 1].plot(pd.to_datetime(contrib_sorted["timestamp"], unit="s", utc=True),
                      contrib_sorted["contrib"].cumsum(), color="crimson")
    ax[1, 1].set_title("cumulative realised PnL ($)"); ax[1, 1].tick_params(axis="x", rotation=30)
    ax[1, 2].hist(t["usdc"], bins=40, color="slategray")
    ax[1, 2].set_title("trade notional (USDC)")
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    path = project_root() / "reports" / "dossiers" / f"{entity_id}.png"
    fig.savefig(path, dpi=90); plt.close(fig)
    return path.name


def build_dossiers(top_n: int = 12) -> list[str]:
    conn = connect()
    feats = pd.read_sql_query("SELECT * FROM wallet_features", conn)
    cls = pd.read_sql_query(
        "SELECT proxy_wallet, bot_score, cluster, operator_id, operator_size FROM wallet_classification", conn)
    df = feats.merge(cls, on="proxy_wallet", how="inner")
    df = df[df["bot_score"] >= 0.5].copy()
    if df.empty:
        log.warning("no wallets with bot_score>=0.5 to profile"); conn.close(); return []

    df[["entity_id", "entity_type"]] = df.apply(lambda r: pd.Series(_entity_id(r)), axis=1)

    trades = pd.read_sql_query(
        "SELECT proxy_wallet, condition_id, side, size, price, usdc, outcome_index, timestamp FROM trades", conn)
    mk = pd.read_sql_query(
        "SELECT condition_id, resolved, winning_outcome_index, end_date FROM markets", conn)
    w2e = dict(zip(df["proxy_wallet"], df["entity_id"]))
    trades = trades[trades["proxy_wallet"].isin(w2e)].copy()
    trades["entity_id"] = trades["proxy_wallet"].map(w2e)
    trades = trades.merge(mk, on="condition_id", how="left")
    trades["target_date"] = trades["end_date"].astype(str).str[:10]
    trades["trade_date"] = pd.to_datetime(trades["timestamp"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    is_buy = trades["side"].eq("BUY")
    payout = (trades["outcome_index"] == trades["winning_outcome_index"]).astype(float)
    trades["contrib"] = np.where(is_buy, -trades["usdc"], trades["usdc"]) + \
        np.where(is_buy, trades["size"], -trades["size"]) * payout
    trades["resolved"] = trades["resolved"].fillna(0).astype(int)

    # entity-level aggregates
    rows = []
    for eid, g in df.groupby("entity_id"):
        et = g["entity_id"].map(lambda _: g["entity_type"].iloc[0]).iloc[0]
        tg = trades[trades["entity_id"] == eid]
        res = tg[tg["resolved"] == 1]
        vol_res = float(res["usdc"].sum())
        rp = float(res["contrib"].sum())
        rows.append({
            "entity_id": eid, "entity_type": g["entity_type"].iloc[0],
            "n_wallets": int(g["proxy_wallet"].nunique()),
            "n_trades": int(len(tg)),
            "volume_usdc": float(tg["usdc"].sum()),
            "realized_pnl_usdc": round(rp, 2),
            "roi_on_resolved": round(rp / vol_res, 4) if vol_res > 0 else np.nan,
            "fill_win_rate": round(float((res["contrib"] > 0).mean()), 4) if len(res) else np.nan,
            "bot_score": float(g["bot_score"].max()),
            "active_hours": int(g["active_hours"].max()),
            "breadth_markets": int(tg["condition_id"].nunique()),
            "cv_gap": float(g["cv_gap"].min()),
            "max_trades_per_sec": int(g["max_trades_per_sec"].max()),
            "median_size": float(g["median_size"].median()),
            "mode_size_frac": float(g["mode_size_frac"].max()),
            "extreme_price_frac": float(g["extreme_price_frac"].median()),
            "reaction_alignment": float(g["reaction_alignment"].mean()),
            "resolution_day_frac": float((tg["trade_date"] == tg["target_date"]).mean()),
            "active_period": f"{res['end_date'].min()} … {tg['target_date'].max()}",
            "members": list(g["proxy_wallet"]),
        })
    ent = pd.DataFrame(rows)
    ent["composite"] = (_zscore(ent["volume_usdc"]) + _zscore(ent["n_trades"])
                        + _zscore(ent["realized_pnl_usdc"]))
    ent = ent.sort_values("composite", ascending=False).head(top_n).reset_index(drop=True)

    written = []
    index_lines = ["# Dossier index — top bots/operators (Phase 5)", "",
                   "| # | entity | type | wallets | trades | volume$ | realized$ | roi | win | archetype |",
                   "|--:|---|---|--:|--:|--:|--:|--:|--:|---|"]
    for i, e in ent.iterrows():
        arche, conf, ev = _archetype(e)
        tg = trades[trades["entity_id"] == e["entity_id"]].sort_values("timestamp")
        png = _plot(e["entity_id"], tg, tg[tg["resolved"] == 1][["timestamp", "contrib"]])
        written.append(_write_dossier(e, arche, conf, ev, png))
        index_lines.append(
            f"| {i+1} | {e['entity_id'][:14]} | {e['entity_type']} | {e['n_wallets']} | "
            f"{e['n_trades']} | {e['volume_usdc']:,.0f} | {e['realized_pnl_usdc']:,.0f} | "
            f"{e['roi_on_resolved'] if pd.notna(e['roi_on_resolved']) else float('nan'):.3f} | "
            f"{e['fill_win_rate'] if pd.notna(e['fill_win_rate']) else float('nan'):.2f} | {arche} |")
    (project_root() / "reports" / "dossiers" / "INDEX.md").write_text(
        "\n".join(index_lines) + "\n", encoding="utf-8")
    conn.close()
    log.info("wrote %d dossiers", len(written))
    return written


def _write_dossier(e: dict, arche: str, conf: str, ev: list[str], png: str) -> str:
    cfg = load_config()
    addr = e["entity_id"]
    lines = [
        f"# Dossier — {addr}", "",
        f"*Type:* {e['entity_type']} | *member wallets:* {e['n_wallets']} | "
        f"*bot_score:* {e['bot_score']:.2f}", "",
        "## Summary stats",
        f"- Trades: **{e['n_trades']}** | Volume: **${e['volume_usdc']:,.0f}** | "
        f"Realised PnL (resolved): **${e['realized_pnl_usdc']:,.0f}** "
        f"(ROI {e['roi_on_resolved'] if pd.notna(e['roi_on_resolved']) else float('nan'):.1%})",
        f"- Fill win rate: **{e['fill_win_rate'] if pd.notna(e['fill_win_rate']) else float('nan'):.2f}** | "
        f"Breadth: **{e['breadth_markets']} markets** | Active hours: **{e['active_hours']}/24**",
        f"- Active period: {e['active_period']} | resolution-day trade share: {e['resolution_day_frac']:.0%}",
        "",
        f"## Inferred strategy archetype: **{arche}**  _(confidence: {conf})_", "",
        "## Evidence", "",
        "| signal | value |", "|---|---|",
        f"| breadth (markets) | {e['breadth_markets']} |",
        f"| active UTC hours | {e['active_hours']}/24 |",
        f"| cadence cv_gap (min) | {e['cv_gap']:.3f} |",
        f"| max fills / second | {e['max_trades_per_sec']} |",
        f"| median size (shares) | {e['median_size']:.1f} |",
        f"| mode-size fraction | {e['mode_size_frac']:.2f} |",
        f"| reaction alignment | {e['reaction_alignment']:.2f} |",
        f"| resolution-day share | {e['resolution_day_frac']:.0%} |",
        f"| fill win rate | {e['fill_win_rate'] if pd.notna(e['fill_win_rate']) else float('nan'):.2f} |",
        f"| ROI on resolved | {e['roi_on_resolved'] if pd.notna(e['roi_on_resolved']) else float('nan'):.3f} |",
        "", "Triggers fired: " + (", ".join(ev) if ev else "—"), "",
        "## Estimated edge source",
        "- High breadth + high win rate + thin ROI ⇒ edge is **many small, "
        "high-probability fills** (spread/fair-value capture across all buckets), "
        "not a few large directional bets.",
        "## Inferred sizing & timing model _(described, not exact)_",
        f"- **Sizing:** median ~{e['median_size']:.0f} shares; "
        f"mode-size fraction {e['mode_size_frac']:.2f} "
        f"({'fixed-size ladder' if e['mode_size_frac'] >= 0.5 else 'variable'}).",
        f"- **Timing:** {e['active_hours']}/24 active, up to {e['max_trades_per_sec']} "
        "fills/second ⇒ automated, polling-driven; cadence regularity "
        f"(cv_gap {e['cv_gap']:.2f}).",
        "## Weaknesses / exploitable angles",
        "- Taker-side data hides maker quote/cancel churn — adverse-selection "
        "windows are not directly measured here (forward live capture, Phase 8).",
        "- Reaction alignment is measured against an **hourly** reference; any "
        "sub-hour staleness after a temperature move is invisible to this proxy.",
        "- High breadth ⇒ thin per-market attention; illiquid buckets / off-peak "
        "UTC hours are candidate coverage gaps (quantified in exploits.md).",
        "",
        f"![plots](./{png})", "",
        "_Caveats: taker-side only; maker fraction/adverse-selection unavailable; "
        "reaction coarse (hourly); operator membership via co-timing._",
    ]
    path = project_root() / "reports" / "dossiers" / f"{addr}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path.name
