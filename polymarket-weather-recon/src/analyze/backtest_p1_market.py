"""P1 variant — MARKET-PRICE-triggered resolution-drift backtest.

Sidesteps the (proven-unfit) weather reference entirely. Thesis: once a bucket's
YES price has climbed above a threshold theta, the *market itself* has singled it
out as the likely winner; buy the residual gap to $1.

Causal: the price crossing is observable live; the resolved outcome is used ONLY
to score PnL. Executable price = the YES print at the crossing (trade-stream
proxy — no historical book; optimistic on queue/slippage). Hold to resolution:
PnL/share = (1 if won else 0) - entry - fee.

Sweeping theta maps the core efficiency trade-off: higher theta -> higher hit
rate but higher entry price -> thinner residual edge. If no theta is net positive
after fees, the resolution-drift edge is NOT capturable by reacting to price
(you'd have to BE the early mover, i.e. P1 needs a faster signal than the book).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..common import normalize as N
from ..common.config import load_config, project_root
from ..common.db import connect


def _sweep(trades: pd.DataFrame, thetas, cap, p) -> dict:
    """θ-sweep over a subset of (already resolution-day-filtered) YES trades."""
    out = {}
    for theta in thetas:
        entries = []
        for cid, g in trades.groupby("condition_id"):
            crossed = g[g["price"] >= theta]
            if crossed.empty:
                continue
            first = crossed.iloc[0]
            notional = min(float(crossed["usdc"].sum()), cap)
            entries.append({
                "entry_price": float(first["price"]),
                "won": int(first["won"]),
                "shares": notional / float(first["price"]) if first["price"] > 0 else 0.0,
                "notional": notional,
                "edge_share": (1.0 if first["won"] else 0.0) - float(first["price"]),
            })
        out[theta] = _metrics(entries, p)
    return out


def run_market_backtest() -> dict:
    cfg = load_config()
    p = cfg["backtest_p1_market"]
    conn = connect()
    trades = pd.read_sql_query(
        "SELECT condition_id, price, usdc, outcome_index, timestamp FROM trades "
        "WHERE outcome_index=0", conn)          # YES leg only
    mk = pd.read_sql_query(
        "SELECT condition_id, city, end_date, winning_outcome_index FROM markets WHERE resolved=1", conn)
    conn.close()

    trades = trades.merge(mk, on="condition_id", how="inner")
    trades["won"] = (trades["winning_outcome_index"] == 0).astype(int)
    trades["trade_date"] = pd.to_datetime(trades["timestamp"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    trades["target_date"] = trades["end_date"].astype(str).str[:10]
    if p["resolution_day_only"]:
        trades = trades[trades["trade_date"] == trades["target_date"]]
    trades = trades.sort_values("timestamp")

    cap = p["per_entry_cap_usdc"]
    thetas = p["thetas"]
    cities = sorted(trades["city"].dropna().unique().tolist())
    nyc = "New York City"
    groups = {
        "ALL": trades,
        "in_sample_NYC": trades[trades["city"] == nyc],
        "out_of_sample": trades[trades["city"] != nyc],
    }
    by_group = {name: _sweep(g, thetas, cap, p) for name, g in groups.items() if not g.empty}
    per_city = {c: _sweep(trades[trades["city"] == c], [p["focus_theta"]], cap, p)
                for c in cities}
    res = {"by_group": by_group, "per_city_focus": per_city,
           "focus_theta": p["focus_theta"], "cities": cities,
           "resolution_day_only": p["resolution_day_only"],
           "n_resolved_markets": int(mk.shape[0])}
    _write(res)
    return res


def _metrics(entries: list[dict], p) -> dict:
    if not entries:
        return {"n": 0}
    df = pd.DataFrame(entries)
    deployed = float(df["notional"].sum())
    def pnl(fee):
        return float(((df["edge_share"] - fee) * df["shares"]).sum())
    p0, pf = pnl(p["taker_fee"]), pnl(p["taker_fee_stress"])
    return {
        "n": int(len(df)),
        "hit_rate": round(float(df["won"].mean()), 4),
        "avg_entry": round(float(df["entry_price"].mean()), 4),
        "avg_edge_share": round(float(df["edge_share"].mean()), 4),
        "deployed_usdc": round(deployed, 0),
        "pnl_fee0": round(p0, 0),
        "roi_fee0": round(p0 / deployed, 4) if deployed else None,
        "pnl_feeStress": round(pf, 0),
        "roi_feeStress": round(pf / deployed, 4) if deployed else None,
    }


def _sweep_table(by_theta: dict, fee_stress) -> list[str]:
    rows = ["| θ | entries | hit rate | avg entry | avg edge/share | deployed$ | PnL(fee0) "
            f"| ROI(fee0) | ROI(fee {fee_stress}) |",
            "|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for theta, m in by_theta.items():
        if m.get("n", 0) == 0:
            rows.append(f"| {theta} | 0 | — | — | — | — | — | — | — |")
            continue
        rows.append(
            f"| {theta} | {m['n']} | {m['hit_rate']:.2f} | {m['avg_entry']:.3f} | "
            f"{m['avg_edge_share']:+.4f} | {m['deployed_usdc']:,.0f} | {m['pnl_fee0']:,.0f} | "
            f"{m['roi_fee0']:.3f} | {m['roi_feeStress']:.3f} |")
    return rows


def _write(res: dict) -> str:
    p = load_config()["backtest_p1_market"]
    oos = res["by_group"].get("out_of_sample", {})
    ins = res["by_group"].get("in_sample_NYC", {})
    ft = res["focus_theta"]
    lines = [
        "# P1 variant — market-price-triggered resolution-drift backtest", "",
        "No weather feed. Trigger: a bucket's YES price crosses **θ** on the resolution day; "
        "enter at that YES print (trade-stream proxy), hold to resolution. Causal — outcome "
        "used only for PnL. **NYC = in-sample; London/Paris/Chicago/Miami = OUT-OF-SAMPLE.**",
        "",
        "## OUT-OF-SAMPLE (London + Paris + Chicago + Miami) — the real test", "",
        *_sweep_table(oos, p["taker_fee_stress"]),
        "",
        "## In-sample (NYC) — for comparison", "",
        *_sweep_table(ins, p["taker_fee_stress"]),
        "",
        f"## Per-city @ θ={ft}", "",
        f"| city | entries | hit rate | avg entry | ROI(fee0) | ROI(fee {p['taker_fee_stress']}) |",
        "|---|--:|--:|--:|--:|--:|",
    ]
    for city, bt in res["per_city_focus"].items():
        m = bt.get(ft, {})
        if m.get("n", 0) == 0:
            lines.append(f"| {city} | 0 | — | — | — | — |")
            continue
        lines.append(f"| {city} | {m['n']} | {m['hit_rate']:.2f} | {m['avg_entry']:.3f} | "
                     f"{m['roi_fee0']:.3f} | {m['roi_feeStress']:.3f} |")

    oos_best = max((m for m in oos.values() if m.get("n")),
                   key=lambda m: (m.get("roi_fee0") or -9), default=None)
    oos_ft = oos.get(ft, {})
    lines += ["", "## Verdict"]
    if oos_best and oos_ft.get("n"):
        gen = "GENERALISES" if (oos_ft.get("roi_feeStress") or -9) > 0 else "DOES NOT generalise"
        lines += [
            f"- Out-of-sample @ θ={ft}: ROI **{oos_ft.get('roi_fee0')}** (fee0) / "
            f"**{oos_ft.get('roi_feeStress')}** (fee {p['taker_fee_stress']}), hit "
            f"{oos_ft.get('hit_rate')}, n={oos_ft.get('n')}.",
            f"- Best out-of-sample θ by ROI(fee0): {oos_best.get('roi_fee0')} "
            f"(hit {oos_best.get('hit_rate')}, entry {oos_best.get('avg_entry')}).",
            f"- **The θ≈{ft} edge {gen} out-of-sample after a stressed fee.** An edge present in "
            "every city (per-city table) is far more trustworthy than one driven by one market.",
        ]
    lines += [
        "", "## Caveats",
        "- Trade-stream proxy (no historical book): assumes the print is takeable with no "
        "queue/slippage and ignores our own market impact — **optimistic**.",
        "- ~58-day window per city; still a backtest, not live. Per-entry capacity capped at "
        f"${p['per_entry_cap_usdc']}.",
        "- A faster *weather* signal (NWS live) would let you enter before full price "
        "convergence, improving entry prices beyond what reacting to the book can achieve.",
    ]
    out = project_root() / "reports" / "backtest_p1_market.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)
