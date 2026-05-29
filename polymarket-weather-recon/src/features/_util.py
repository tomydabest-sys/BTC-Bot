"""Shared feature helpers."""
from __future__ import annotations

import numpy as np


def shannon_entropy(counts) -> float:
    """Entropy in bits of a count vector. 0 for a single bucket / empty."""
    c = np.asarray(list(counts), dtype=float)
    c = c[c > 0]
    if c.size <= 1:
        return 0.0
    p = c / c.sum()
    return float(-(p * np.log2(p)).sum())


def cv(x) -> float:
    """Coefficient of variation; 0 when mean is 0 or <2 points."""
    a = np.asarray(x, dtype=float)
    if a.size < 2 or a.mean() == 0:
        return 0.0
    return float(a.std() / abs(a.mean()))
