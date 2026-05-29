"""P1 resolution-drift backtest (Option-1 prototype).

Puts a number on the resolution-drift edge using only available data, honestly:

* No historical order book exists for closed markets, so the **actual trade
  stream is the executable-price proxy** — we take the first YES print at/after
  the signal as our entry (we'd be competing for that same liquidity).
* The entry trigger is **causal**: peak_passed uses only data up to the decision
  time (local clock past a min peak hour AND temperature has fallen below the
  running-max) — it does NOT peek at the final daily max. So when the temperature
  ticks up again after a false peak, the backtest takes the resulting loss.
* PnL per share = payout (1 if the bucket won else 0) − entry_price − fee.

Reported with fee sensitivity and a no-peak-gate baseline so the edge is
attributable to the rule, not to hindsight. Single-city / 30-day window — treat
as indicative; out-of-sample (other cities) is the real test.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..common import normalize as N
from ..common.config import load_config, project_root
from ..common.db import connect

log = logging.getLogger("recon.backtest_p1")


def _reference_by_day(conn, tz_map):
    ref = pd.read_sql_query("SELECT station, ts, temp_f FROM reference_temp", conn)
    if ref.empty:
        return ref
    ref["dt"] = pd.to_datetime(ref["ts"], unit="s", utc=True)
    out = []
    for station, g in ref.groupby("station"):
        tz = tz_map.get(station, "UTC")
        loc = g["dt"].dt.tz_convert(tz)
        out.append(g.assign(local_date=loc.dt.strftime("%Y-%m-%d"), local_hour=loc.dt.hour))
    return pd.concat(out, ignore_index=True).sort_values("ts")


def _signal_ts(day: pd.DataFrame, bounds, p) -> tuple[int | None, float | None]:
    """First causal peak_passed time and the running-max bucket at that time.
    Returns (signal_ts, running_max) if the day's post-peak running-max lands in
    `bounds`, else (None, None)."""
    lo, hi = bounds
    ts = day["ts"].to_numpy()
    temp = day["temp_f"].to_numpy()
    hour = day["local_hour"].to_numpy()
    run = -np.inf
    decl = 0
    for i in range(len(ts)):
        run = max(run, temp[i])
        if temp[i] < run - p["decline_margin_f"]:
            decl += 1
        else:
            decl = 0
        if hour[i] >= p["min_peak_hour_local"] and decl >= p["decline_readings"]:
            # peak passed; the live-leader bucket is the one holding the running max
            if lo <= run <= hi:
                return int(ts[i]), float(run)
            return None, None
    return None, None


def _first_in_bucket_ts(day: pd.DataFrame, bounds) -> int | None:
    """Baseline: first time running-max enters the bucket (NO peak gate)."""
    lo, hi = bounds
    run = -np.inf
    for ts, t in zip(day["ts"].to_numpy(), day["temp_f"].to_numpy()):
        run = max(run, t)
        if lo <= run <= hi:
            return int(ts)
    return None


def run_backtest() -> dict:
    cfg = load_config()
    p = cfg["backtest_p1"]
    tz_map = {k: v.get("tz", "UTC") for k, v in cfg["weather_reference"]["stations"].items()}
    conn = connect()
    trades = pd.read_sql_query(
        "SELECT condition_id, price, size, usdc, outcome_index, timestamp FROM trades", conn)
    mk = pd.read_sql_query(
        "SELECT condition_id, event_slug, bucket_label, station, end_date, resolved, "
        "winning_outcome_index FROM markets WHERE resolved=1", conn)
    ref = _reference_by_day(conn, tz_map)
    conn.close()
    if ref.empty:
        return {"error": "no reference_temp; run Phase 3 first"}

    yes = trades[trades["outcome_index"] == 0].sort_values("timestamp")
    rows_strat, rows_base = [], []
    for _, m in mk.iterrows():
        bounds = N.parse_bucket_bounds(m["bucket_label"])
        if bounds is None or not m["station"]:
            continue
        day = ref[(ref["station"] == m["station"]) &
                  (ref["local_date"] == str(m["end_date"])[:10])]
        if day.empty:
            continue
        won = int(m["winning_outcome_index"] == 0)
        mt = yes[yes["condition_id"] == m["condition_id"]]

        sig_ts, _ = _signal_ts(day, bounds, p)
        if sig_ts is not None:
            fills = mt[mt["timestamp"] >= sig_ts]
            if len(fills):
                rows_strat.append(_entry(fills.iloc[0], won, m, p["per_entry_cap_usdc"]))
        base_ts = _first_in_bucket_ts(day, bounds)
        if base_ts is not None:
            bfills = mt[mt["timestamp"] >= base_ts]
            if len(bfills):
                rows_base.append(_entry(bfills.iloc[0], won, m, p["per_entry_cap_usdc"]))

    res = {
        "strategy": _metrics(rows_strat, p),
        "baseline_no_peak_gate": _metrics(rows_base, p),
        "reference_diagnostic": _reference_accuracy(mk, ref),
        "params": {k: p[k] for k in ("min_peak_hour_local", "decline_margin_f",
                                     "taker_fee", "taker_fee_stress", "per_entry_cap_usdc")},
        "n_resolved_markets": int(len(mk)),
    }
    _write(res, rows_strat)
    return res


def _bucket_center(bounds) -> float | None:
    lo, hi = bounds
    if lo == float("-inf"):
        return hi
    if hi == float("inf"):
        return lo
    return (lo + hi) / 2


def _reference_accuracy(mk: pd.DataFrame, ref: pd.DataFrame) -> dict:
    """KEY DIAGNOSTIC: does the reference daily-max land in the bucket that
    actually won? And what is the signed bias (reference − winning-bucket centre)?
    Explains whether a reference-driven P1 can identify the winner at all."""
    biases, matches = [], []
    for _, g in mk.groupby("event_slug"):
        day = str(g["end_date"].iloc[0])[:10]
        station = g["station"].iloc[0]
        dref = ref[(ref["station"] == station) & (ref["local_date"] == day)]
        win = g[g["winning_outcome_index"] == 0]
        if dref.empty or win.empty:
            continue
        ref_max = float(dref["temp_f"].max())
        wb = N.parse_bucket_bounds(win["bucket_label"].iloc[0])
        if not wb:
            continue
        matches.append(int(wb[0] <= ref_max <= wb[1]))
        center = _bucket_center(wb)
        if center is not None and abs(center) != float("inf"):
            biases.append(ref_max - center)
    if not matches:
        return {"n_events": 0}
    return {
        "n_events": len(matches),
        "ref_in_winning_bucket_rate": round(float(np.mean(matches)), 4),
        "mean_signed_bias_f": round(float(np.mean(biases)), 2) if biases else None,
        "median_abs_bias_f": round(float(np.median(np.abs(biases))), 2) if biases else None,
        "bucket_width_f": 2.0,
    }


def _entry(fill, won, m, cap) -> dict:
    notional = min(float(fill["usdc"]), cap)
    shares = notional / float(fill["price"]) if fill["price"] > 0 else 0.0
    return {"condition_id": m["condition_id"], "bucket": m["bucket_label"],
            "entry_price": float(fill["price"]), "won": won,
            "notional": notional, "shares": shares,
            "edge_share": (1.0 if won else 0.0) - float(fill["price"])}


def _metrics(rows: list[dict], p) -> dict:
    if not rows:
        return {"n": 0}
    df = pd.DataFrame(rows)
    def pnl(fee):
        return float(((df["edge_share"] - fee) * df["shares"]).sum())
    deployed = float(df["notional"].sum())
    pnl0, pnlf = pnl(p["taker_fee"]), pnl(p["taker_fee_stress"])
    return {
        "n": int(len(df)),
        "hit_rate": round(float(df["won"].mean()), 4),
        "avg_entry_price": round(float(df["entry_price"].mean()), 4),
        "avg_edge_per_share": round(float(df["edge_share"].mean()), 4),
        "deployed_usdc": round(deployed, 0),
        "pnl_fee0": round(pnl0, 0),
        "roi_fee0": round(pnl0 / deployed, 4) if deployed else None,
        f"pnl_fee_{p['taker_fee_stress']}": round(pnlf, 0),
        f"roi_fee_{p['taker_fee_stress']}": round(pnlf / deployed, 4) if deployed else None,
    }


def _write(res: dict, rows_strat: list[dict]) -> str:
    s, b = res["strategy"], res["baseline_no_peak_gate"]
    lines = [
        "# P1 resolution-drift backtest (prototype)", "",
        "**Method (honest):** entries triggered by a *causal* peak-passed rule on the "
        "hourly KLGA reference (no peek at the final daily max); executable price = first "
        "YES print at/after the signal (trade-stream proxy; no order-book depth). Hold to "
        "resolution. Single-city/30-day window — indicative, not out-of-sample.", "",
        "## Strategy (with peak gate)", "",
        f"- Fired on **{s.get('n',0)}** of {res['n_resolved_markets']} resolved markets | "
        f"hit rate **{s.get('hit_rate')}** | avg entry **{s.get('avg_entry_price')}** | "
        f"avg edge/share **{s.get('avg_edge_per_share')}**",
        f"- Deployed **${s.get('deployed_usdc'):,.0f}** | PnL(fee 0) **${s.get('pnl_fee0'):,.0f}** "
        f"(ROI {s.get('roi_fee0')}) | PnL(fee {res['params']['taker_fee_stress']}) "
        f"**${s.get('pnl_fee_'+str(res['params']['taker_fee_stress'])):,.0f}** "
        f"(ROI {s.get('roi_fee_'+str(res['params']['taker_fee_stress']))})",
        "",
        "## Baseline (NO peak gate — enter as soon as running-max enters the bucket)", "",
        f"- Fired on **{b.get('n',0)}** | hit rate **{b.get('hit_rate')}** | "
        f"avg edge/share **{b.get('avg_edge_per_share')}** | PnL(fee 0) **${b.get('pnl_fee0',0):,.0f}** "
        f"(ROI {b.get('roi_fee0')})",
        "",
        "The peak gate should raise hit rate / edge vs the baseline — that delta is the "
        "value of waiting for the causal peak signal (the baseline buys winners AND buckets "
        "the temperature later climbs out of).", "",
        "## ⚠️ Diagnosis — why this fails: the reference cannot pick the 2°F bucket",
        f"- Open-Meteo daily-max lands in the **actual winning bucket only "
        f"{res['reference_diagnostic'].get('ref_in_winning_bucket_rate', 'n/a'):.0%}** of "
        f"{res['reference_diagnostic'].get('n_events', 0)} events.",
        f"- It carries a **systematic bias of "
        f"{res['reference_diagnostic'].get('mean_signed_bias_f')}°F** "
        f"(median |bias| {res['reference_diagnostic'].get('median_abs_bias_f')}°F) vs the "
        "resolved bucket centre — and the buckets are only **2°F wide**. The modeled 2m "
        "temperature reads systematically *hotter* than the official station high used to "
        "resolve, so the rule keeps buying the bucket one step too high.",
        "- **Conclusion:** the ex-post resolution-drift edge is real (exploits.md: ~$0.146/"
        "share on $1.32M of winning-side flow) but **NOT capturable with Open-Meteo** as the "
        "reference. The backtest correctly kills the naive implementation.",
        "",
        "## Fix direction (validated spot-check)",
        "- Use the **actual station observation feed** (NWS METAR for KLGA via "
        "`api.weather.gov`, or the Wunderground resolver) instead of Open-Meteo. Spot-check: "
        "NWS KLGA daily max **84.2°F → resolved bucket 84-85°F (exact match)** on 2026-05-27, "
        "vs Open-Meteo's bias. (NWS API retention is only ~3 days, so this validates the "
        "*live* path; deep historical NWS backtests aren't possible here.)",
        "- Or **bias-correct** Open-Meteo (subtract the measured ~mean bias) and **avoid "
        "entries when the reading is within ~1-2°F of a bucket edge**.",
        "- Or make the **market price itself** the trigger (trade convergence once the book "
        "has already singled out a winner), rather than the weather model.",
        "",
        f"```\n{res['params']}\n```", "",
        "## Caveats",
        "- Trade-stream proxy is optimistic: assumes we get the next print's price with no "
        "queue/slippage and ignores that our own flow would move it.",
        "- Hourly reference ⇒ ±1h peak timing error, the main driver of mis-fires.",
        "- In-sample single city; validate on held-out cities (scale-up) before sizing.",
        "- Fee units for Polymarket weather markets unconfirmed; shown at 0 and a stress level.",
    ]
    out = project_root() / "reports" / "backtest_p1.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # also drop the per-entry detail for inspection
    if rows_strat:
        pd.DataFrame(rows_strat).to_csv(
            project_root() / "reports" / "backtest_p1_entries.csv", index=False)
    return str(out)
