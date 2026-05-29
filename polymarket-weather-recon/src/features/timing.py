"""Phase 3 — timing / automation features (per wallet).

Note: Data API timestamps are SECOND-resolution, so sub-second cadence is not
measurable. Burstiness is therefore capped at 1s granularity (same-second
clusters), which we state honestly rather than claiming sub-second precision.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ._util import cv, shannon_entropy


def timing_features(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for wallet, g in trades.sort_values("timestamp").groupby("proxy_wallet"):
        ts = g["timestamp"].to_numpy()
        gaps = np.diff(ts)
        gaps_pos = gaps[gaps > 0]
        dts = pd.to_datetime(ts, unit="s", utc=True)
        sec = (ts % 60)
        minute = (ts // 60) % 60
        hour = dts.hour.to_numpy()
        # same-second burst: trades sharing a timestamp with another by this wallet
        _, counts = np.unique(ts, return_counts=True)
        same_sec = counts[counts > 1].sum()
        rows.append({
            "proxy_wallet": wallet,
            "n_trades": len(g),
            "active_days": dts.normalize().nunique(),
            "median_gap_s": float(np.median(gaps_pos)) if gaps_pos.size else np.nan,
            "iqr_gap_s": float(np.subtract(*np.percentile(gaps_pos, [75, 25]))) if gaps_pos.size else np.nan,
            "cv_gap": cv(gaps_pos) if gaps_pos.size else 0.0,
            "sec_of_min_entropy": shannon_entropy(np.bincount(sec, minlength=60)),
            "min_of_hour_entropy": shannon_entropy(np.bincount(minute, minlength=60)),
            "hour_entropy": shannon_entropy(np.bincount(hour, minlength=24)),
            "active_hours": int(np.unique(hour).size),
            "nighttime_ratio_utc": float(np.isin(hour, range(0, 7)).mean()),
            "max_trades_per_sec": int(counts.max()) if counts.size else 0,
            "same_second_burst_frac": float(same_sec / len(g)) if len(g) else 0.0,
        })
    return pd.DataFrame(rows)
