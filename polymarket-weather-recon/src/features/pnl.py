"""Phase 3 — settlement-based realised PnL (per wallet).

Marks each taker fill to RESOLUTION (winning outcome pays $1, losing $0), which
is a well-defined realised PnL for resolved markets and far less noisy than the
open-position snapshot proxy (see weather_wallet_analysis.md). Per fill:

    cashflow = -usdc (BUY) | +usdc (SELL)
    terminal = (+size BUY | -size SELL) * payout,  payout = 1 if won else 0
    pnl      = cashflow + terminal

Summing per-fill pnl correctly nets buys/sells and round-trips. Only fills in
RESOLVED markets are counted; unresolved-market exposure is reported separately.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def settlement_pnl(trades: pd.DataFrame, markets: pd.DataFrame) -> pd.DataFrame:
    m = markets[["condition_id", "resolved", "winning_outcome_index"]]
    df = trades.merge(m, on="condition_id", how="left")
    df["resolved"] = df["resolved"].fillna(0).astype(int)
    is_buy = df["side"].eq("BUY")
    cashflow = np.where(is_buy, -df["usdc"], df["usdc"])
    signed_shares = np.where(is_buy, df["size"], -df["size"])
    payout = (df["outcome_index"] == df["winning_outcome_index"]).astype(float)
    contrib = cashflow + signed_shares * payout
    df = df.assign(_contrib=contrib, _resolved=df["resolved"])

    out = []
    for wallet, g in df.groupby("proxy_wallet"):
        res = g[g["_resolved"] == 1]
        vol_all = float(g["usdc"].sum())
        vol_res = float(res["usdc"].sum())
        rp = float(res["_contrib"].sum())
        out.append({
            "proxy_wallet": wallet,
            "volume_usdc": round(vol_all, 2),
            "volume_resolved_usdc": round(vol_res, 2),
            "realized_pnl_usdc": round(rp, 2),
            "roi_on_resolved": round(rp / vol_res, 4) if vol_res > 0 else np.nan,
            "n_trades_resolved": int(len(res)),
            "fill_win_rate": round(float((res["_contrib"] > 0).mean()), 4) if len(res) else np.nan,
            "frac_trades_resolved": round(float(len(res) / len(g)), 4) if len(g) else np.nan,
        })
    return pd.DataFrame(out)
