"""Phase 4 — heuristic bot score.

Transparent linear combination of 0-1 automation sub-scores (weights in
config.yaml -> thresholds.bot_score.weights). Per-component contributions are
emitted alongside the total so the score is fully auditable. Wallets with fewer
than `min_score_trades` fills are left 'unscored' (too little evidence).

Not implemented this pass (stated honestly): fixed-offset pricing vs a prevailing
mid — robust mid reconstruction for closed markets is coarse (CLOB prices-history
density tracks activity), so it is deferred rather than faked.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..common.config import load_config


def _clip01(x):
    return np.clip(x, 0.0, 1.0)


def score_wallets(feats: pd.DataFrame) -> pd.DataFrame:
    th = load_config()["thresholds"]["bot_score"]
    w = th["weights"]
    f = feats.copy()

    scored = f["n_trades"] >= th["min_score_trades"]

    c_cadence = _clip01(1.0 - f["cv_gap"] / th["cv_gap_machine"])
    c_activity = _clip01(f["active_hours"] / 24.0)
    c_size = _clip01(f["mode_size_frac"].fillna(0))
    c_breadth = _clip01(f["breadth_markets"] / th["breadth_systematic"])
    c_burst = _clip01(f["max_trades_per_sec"] / th["burst_machine_per_sec"])
    react_ok = f["n_reaction_classifiable"].fillna(0) >= th["reaction_min_classifiable"]
    c_react = np.where(react_ok, _clip01((f["reaction_alignment"].fillna(0.5) - 0.5) * 2.0), 0.0)

    f["c_cadence_regularity"] = c_cadence
    f["c_activity_247"] = c_activity
    f["c_size_regularity"] = c_size
    f["c_breadth"] = c_breadth
    f["c_burstiness"] = c_burst
    f["c_reaction"] = c_react

    total = (w["cadence_regularity"] * c_cadence
             + w["activity_247"] * c_activity
             + w["size_regularity"] * c_size
             + w["breadth"] * c_breadth
             + w["burstiness"] * c_burst
             + w["reaction"] * c_react)
    f["bot_score"] = np.where(scored, _clip01(total), np.nan)
    f["scored"] = scored
    return f
