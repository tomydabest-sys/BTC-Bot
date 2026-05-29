"""Phase 3.5 — reaction / directional-alignment features (per wallet).

COARSE by construction (hourly reference; second-resolution trades). We do NOT
claim a sub-second latency budget. Instead we measure, for trades that happen
once the resolution-day temperature is being realised, whether the wallet trades
in the direction the *realised* running-max implies:

  running_max in  [lo, hi]  -> this bucket is the live leader (bullish)
  running_max >   hi         -> temp has passed this bucket (bearish)
  running_max <   lo         -> temp not there yet (unclassifiable, skipped)

aligned   = bullish state & (BUY YES | SELL NO)  OR  bearish state & (SELL YES | BUY NO)
alignment = aligned / (aligned + misaligned)

Resolution-drift / feed-driven traders score high (≈1); noise ≈0.5. Confidence
LOW–MEDIUM. outcome_index: 0 = YES, 1 = NO.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..common import normalize as N


def reaction_features(trades: pd.DataFrame, markets: pd.DataFrame,
                      reference: pd.DataFrame, tz_map: dict[str, str]) -> pd.DataFrame:
    if reference.empty:
        return pd.DataFrame(columns=["proxy_wallet", "reaction_alignment", "n_reaction_classifiable"])

    # reference points -> local date per station
    ref = reference.copy()
    ref["dt"] = pd.to_datetime(ref["ts"], unit="s", utc=True)
    parts = []
    for station, g in ref.groupby("station"):
        tz = tz_map.get(station, "UTC")
        local_date = g["dt"].dt.tz_convert(tz).dt.strftime("%Y-%m-%d")
        parts.append(g.assign(local_date=local_date))
    ref = pd.concat(parts, ignore_index=True).sort_values("ts")

    mk = markets.set_index("condition_id")
    counts: dict[str, list[int]] = {}  # wallet -> [aligned, misaligned]

    for cid, g in trades.groupby("condition_id"):
        if cid not in mk.index:
            continue
        row = mk.loc[cid]
        bounds = N.parse_bucket_bounds(row.get("bucket_label"))
        station = row.get("station")
        if bounds is None or not station:
            continue
        lo, hi = bounds
        target_date = str(row.get("end_date"))[:10]
        day = ref[(ref["station"] == station) & (ref["local_date"] == target_date)]
        if day.empty:
            continue
        day_ts = day["ts"].to_numpy()
        day_cummax = np.maximum.accumulate(day["temp_f"].to_numpy())

        gg = g.sort_values("timestamp")
        idx = np.searchsorted(day_ts, gg["timestamp"].to_numpy(), side="right") - 1
        valid = idx >= 0
        if not valid.any():
            continue
        run_max = np.where(valid, day_cummax[np.clip(idx, 0, len(day_cummax) - 1)], np.nan)
        is_buy = gg["side"].eq("BUY").to_numpy()
        is_yes = (gg["outcome_index"] == 0).to_numpy()
        bullish_state = valid & (run_max >= lo) & (run_max <= hi)
        bearish_state = valid & (run_max > hi)
        # trader bullish on bucket = BUY YES or SELL NO
        trader_bullish = (is_buy & is_yes) | (~is_buy & ~is_yes)
        aligned = (bullish_state & trader_bullish) | (bearish_state & ~trader_bullish)
        misaligned = (bullish_state & ~trader_bullish) | (bearish_state & trader_bullish)

        wallets = gg["proxy_wallet"].to_numpy()
        for w, a, m in zip(wallets, aligned, misaligned):
            if a or m:
                c = counts.setdefault(w, [0, 0])
                c[0] += int(a)
                c[1] += int(m)

    rows = []
    for w, (a, m) in counts.items():
        tot = a + m
        rows.append({
            "proxy_wallet": w,
            "reaction_alignment": round(a / tot, 4) if tot else np.nan,
            "n_reaction_classifiable": tot,
        })
    return pd.DataFrame(rows)
