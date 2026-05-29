"""Phase 3 — sizing + price-signature features (per wallet)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ._util import shannon_entropy


def sizing_features(trades: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for wallet, g in trades.groupby("proxy_wallet"):
        size = g["size"].to_numpy(dtype=float)
        price = g["price"].to_numpy(dtype=float)
        n = len(g)
        vc = g["size"].round(4).value_counts()
        rows.append({
            "proxy_wallet": wallet,
            "distinct_sizes": int(vc.size),
            "distinct_size_ratio": float(vc.size / n) if n else np.nan,
            "round_size_frac": float(np.mean(np.isclose(size, np.round(size)))),
            "mode_size_frac": float(vc.iloc[0] / n) if n else np.nan,
            "median_size": float(np.median(size)),
            "iqr_size": float(np.subtract(*np.percentile(size, [75, 25]))),
            "median_usdc": float(np.median(g["usdc"].to_numpy(dtype=float))),
            "mean_price": float(price.mean()),
            "extreme_price_frac": float(np.mean((price < 0.05) | (price > 0.95))),
            "price_entropy": shannon_entropy(np.histogram(price, bins=20, range=(0, 1))[0]),
        })
    return pd.DataFrame(rows)
