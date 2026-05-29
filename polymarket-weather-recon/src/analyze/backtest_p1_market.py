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


def run_market_backtest() -> dict:
    cfg = load_config()
    p = cfg["backtest_p1_market"]
    conn = connect()
    trades = pd.read_sql_query(
        "SELECT condition_id, price, usdc, outcome_index, timestamp FROM trades "
        "WHERE outcome_index=0", conn)          # YES leg only
    mk = pd.read_sql_query(
        "SELECT condition_id, end_date, winning_outcome_index FROM markets WHERE resolved=1", conn)
    conn.close()

    trades = trades.merge(mk, on="condition_id", how="inner")
    trades["won"] = (trades["winning_outcome_index"] == 0).astype(int)
    trades["trade_date"] = pd.to_datetime(trades["timestamp"], unit="s", utc=True).dt.strftime("%Y-%m-%d")
    trades["target_date"] = trades["end_date"].astype(str).str[:10]
    if p["resolution_day_only"]:
        trades = trades[trades["trade_date"] == trades["target_date"]]
    trades = trades.sort_values("timestamp")

    cap = p["per_entry_cap_usdc"]
    by_theta = {}
    for theta in p["thetas"]:
        entries = []
        for cid, g in trades.groupby("condition_id"):
            crossed = g[g["price"] >= theta]
            if crossed.empty:
                continue
            first = crossed.iloc[0]
            # capacity = YES notional available at/after the crossing
            avail = float(crossed["usdc"].sum())
            notional = min(avail, cap)
            entries.append({
                "entry_price": float(first["price"]),
                "won": int(first["won"]),
                "shares": notional / float(first["price"]) if first["price"] > 0 else 0.0,
                "notional": notional,
                "edge_share": (1.0 if first["won"] else 0.0) - float(first["price"]),
            })
        by_theta[theta] = _metrics(entries, p)
    res = {"by_theta": by_theta, "resolution_day_only": p["resolution_day_only"],
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


def _write(res: dict) -> str:
    p = load_config()["backtest_p1_market"]
    lines = [
        "# P1 variant — market-price-triggered resolution-drift backtest", "",
        "No weather feed. Trigger: a bucket's YES price crosses **θ** on the resolution "
        f"day ({'resolution-day only' if res['resolution_day_only'] else 'any day'}); enter at "
        "that YES print (trade-stream proxy), hold to resolution. Causal — outcome used only "
        "for PnL. Single-city/30-day window — indicative.", "",
        "| θ | entries | hit rate | avg entry | avg edge/share | deployed$ | PnL(fee0) | ROI(fee0) | ROI(fee "
        f"{p['taker_fee_stress']}) |",
        "|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for theta, m in res["by_theta"].items():
        if m.get("n", 0) == 0:
            lines.append(f"| {theta} | 0 | — | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {theta} | {m['n']} | {m['hit_rate']:.2f} | {m['avg_entry']:.3f} | "
            f"{m['avg_edge_share']:+.4f} | {m['deployed_usdc']:,.0f} | {m['pnl_fee0']:,.0f} | "
            f"{m['roi_fee0']:.3f} | {m['roi_feeStress']:.3f} |")
    # interpretation
    best = max((m for m in res["by_theta"].values() if m.get("n")),
               key=lambda m: (m.get("roi_fee0") or -9), default=None)
    lines += ["",
              "## Read",
              "- **Higher θ → higher hit rate but higher entry price** (thinner residual gap to "
              "$1). The question is whether any θ stays **net positive after fees**.",
              ]
    if best:
        sign = "POSITIVE" if (best.get("roi_fee0") or 0) > 0 else "NEGATIVE"
        lines.append(
            f"- Best θ by ROI(fee0) gives ROI **{best.get('roi_fee0')}** "
            f"(hit {best.get('hit_rate')}, entry {best.get('avg_entry')}) — **{sign}** at zero "
            f"fee; under the stress fee {p['taker_fee_stress']} ROI is {best.get('roi_feeStress')}.")
    lines += [
        "- If even the best θ is ~0/negative after fees, reacting to the book is **too late** — "
        "the convergence is already priced. Then P1 only works with a signal *faster than the "
        "market* (the resolution-drift snipers' actual edge), not by following price.",
        "",
        "## Caveats",
        "- Trade-stream proxy (no historical book): assumes the print price is takeable with no "
        "queue/slippage and ignores our own market impact — **optimistic**.",
        "- Single city / 30 days, in-sample; validate out-of-sample before sizing.",
        "- Capacity = YES notional transacting at/after the crossing (per-entry cap "
        f"${p['per_entry_cap_usdc']}).",
    ]
    out = project_root() / "reports" / "backtest_p1_market.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)
