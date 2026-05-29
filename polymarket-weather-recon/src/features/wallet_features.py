"""Phase 3 — per-wallet feature vectors (STUB).

Assemble per-market and aggregated feature vectors per wallet from the
normalised trades table, combining the timing / sizing / pnl / reaction
sub-modules. Prefer robust stats (median, IQR, entropy) over means.

Caveat from Phase 0: features are computed over TAKER-SIDE fills only (maker
counterparty is not in the available data), so "maker fraction" and
adverse-selection-as-maker features are approximate and must be labelled as
inferred, not observed.
"""
from __future__ import annotations


def build_wallet_features(*args, **kwargs):
    raise NotImplementedError("Phase 3 — pending Phase 0/2 checkpoints")
