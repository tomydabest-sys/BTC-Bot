"""Phase 3 — PnL / quality features (STUB).

Realised + unrealised PnL (Data API /positions: realizedPnl + cashPnl as a
proxy), per-trade PnL distribution, win rate, volume, turnover, and adverse
selection (signed mid move at horizon h after each fill).

Caveat from Phase 0: snapshot PnL from /positions is noisy while positions are
open; closed round-trip PnL is the trustworthy measure (see the repo's
weather_wallet_analysis.md for why snapshot proxies mislead).
"""
from __future__ import annotations


def pnl_features(*args, **kwargs):
    raise NotImplementedError("Phase 3 — pending checkpoints")
