"""Shared fair-value and volatility math.

This module replaces the old sigmoid `P(up) = 1/(1+exp(-k*move))` model.
The sigmoid saturates to ~0.5 for the sub-0.05% BTC moves that dominate
the 5-min market regime, which is the root cause of the bot's silent
zero-trade behavior.

Black-Scholes binary `N(d2)` driven by realized EWMA volatility is
mathematically the right model for "will spot >= strike at expiry?"
on BTC up/down markets.

Key functions:
    fair_value(spot, strike, t_rem_s, sigma_per_sec) -> [0, 1]
    realized_sigma_per_sec(price_buf, lookback_s, halflife_s) -> float
    fee_at_price(p, theta=0.072) -> float
    fee_aware_edge(model_p, mid, exit_p_estimate, theta) -> float
"""

from __future__ import annotations

import math
from collections import deque
from statistics import NormalDist
from typing import Iterable, Sequence

_N = NormalDist().cdf


def fair_value(
    spot: float,
    strike: float,
    t_rem_s: float,
    sigma_per_sec: float,
) -> float:
    """Closed-form binary digital fair value: P(spot_T >= strike).

    Uses zero-drift geometric Brownian motion (drift << diffusion at sub-hour
    horizons for BTC). Returns the cumulative normal of d2.

    Args:
        spot: current underlying price (BTC/USD)
        strike: market strike — typically the 5-min window open price
        t_rem_s: seconds remaining until resolution
        sigma_per_sec: per-second log-return volatility (see realized_sigma_per_sec)

    Returns:
        Probability in [0.02, 0.98] (clamped to avoid degenerate edge cases).
    """
    if spot <= 0 or strike <= 0:
        return 0.5
    if t_rem_s <= 0:
        return 1.0 if spot >= strike else 0.0
    if sigma_per_sec <= 0:
        return 1.0 if spot >= strike else 0.0
    sigma_t = sigma_per_sec * math.sqrt(t_rem_s)
    if sigma_t < 1e-12:
        return 1.0 if spot >= strike else 0.0
    try:
        d2 = math.log(spot / strike) / sigma_t
    except (ValueError, ZeroDivisionError):
        return 0.5
    p = _N(d2)
    return max(0.02, min(0.98, p))


def realized_sigma_per_sec(
    price_buf: Sequence[float],
    lookback_s: int = 900,
    halflife_s: int = 60,
    fallback_annualized: float = 0.45,
) -> float:
    """EWMA per-second volatility of log returns over the last N seconds.

    Args:
        price_buf: ordered sequence of 1-second BTC prices (most recent last).
        lookback_s: how far back to look (seconds). Default 900 = 15 min.
        halflife_s: EWMA half-life. Default 60s — responsive but smooth.
        fallback_annualized: returned when buffer too short. Default 45% ≈
            spring-2026 BTC realized vol regime.

    Returns:
        Per-second sigma (e.g. 1e-4 means 1bp/sec). Always positive.
    """
    if not price_buf or len(price_buf) < 30:
        # Convert annualized vol to per-second
        seconds_per_year = 365.0 * 24.0 * 3600.0
        return fallback_annualized / math.sqrt(seconds_per_year)

    # Take the last `lookback_s` samples (assumes 1-Hz buffer)
    sample = list(price_buf[-lookback_s:])
    if len(sample) < 3:
        seconds_per_year = 365.0 * 24.0 * 3600.0
        return fallback_annualized / math.sqrt(seconds_per_year)

    # Log returns
    rets = []
    for i in range(1, len(sample)):
        prev, cur = sample[i - 1], sample[i]
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))

    if len(rets) < 5:
        seconds_per_year = 365.0 * 24.0 * 3600.0
        return fallback_annualized / math.sqrt(seconds_per_year)

    # EWMA variance — alpha derived from half-life: alpha = 1 - 2^(-1/halflife)
    alpha = 1.0 - 2.0 ** (-1.0 / max(halflife_s, 1))
    var = 0.0
    for r in rets:
        var = alpha * (r * r) + (1.0 - alpha) * var

    sigma = math.sqrt(max(var, 1e-20))
    # Floor at 1e-7 (1bp/sec/100) to avoid degenerate fair values
    return max(sigma, 1e-7)


def fee_at_price(p: float, theta: float = 0.072) -> float:
    """Polymarket fee approximation: Fee = theta * p * (1-p).

    The default theta=0.072 is tuned so the peak fee at p=0.5 is ~1.80%, the
    documented max fee for global crypto markets per Polymarket docs and
    multiple aggregators (Apr 2026).

    For 15m/5m crypto markets where dynamic fees can spike to ~3.15%, pass
    theta=0.126.

    For maker side: fee is ~0% (rebated). Pass theta=0.0 if posting maker.

    Args:
        p: trade price in [0, 1].
        theta: fee curvature constant. Default = global taker.
    Returns:
        Fee as a fraction of notional (e.g. 0.018 = 1.8%).
    """
    p = max(0.0, min(1.0, p))
    return theta * p * (1.0 - p)


def fee_aware_edge(
    model_p: float,
    mid: float,
    exit_p_estimate: float = 0.5,
    theta: float = 0.072,
) -> float:
    """Round-trip fee-adjusted edge.

    Args:
        model_p: model's probability estimate (fair value).
        mid: current market mid price.
        exit_p_estimate: where you expect to exit. Default 0.5 (worst-case).
        theta: fee curvature. Pass 0.0 for maker-side trades.

    Returns:
        Net edge after entry+exit fees, as a fraction of notional.
        Positive = trade has positive expected value before slippage.
    """
    raw_edge = model_p - mid
    fee_in = fee_at_price(mid, theta)
    fee_out = fee_at_price(exit_p_estimate, theta)
    return raw_edge - fee_in - fee_out


# ─────────────────────────────────────────────────────────────────────────────
#  Micro-lag predictor — sub-second momentum extrapolation
# ─────────────────────────────────────────────────────────────────────────────


def micro_lag_predict(
    price_buf_subsec: Sequence[float],
    horizon_s: float = 2.0,
    sample_period_s: float = 0.1,
    rho_clip: float = 0.20,
    decay: float = 0.5,
) -> float:
    """Predict BTC price `horizon_s` ahead using AR(1) on short-window returns.

    Uses Tartakovsky-style sub-1-minute autocorrelation (typically rho_1 in
    [0.02, 0.10]) to forecast the next 1–3 seconds. Clipped conservatively
    so noise doesn't cause large excursions.

    Args:
        price_buf_subsec: ordered sub-second price buffer (most recent last).
            E.g. 100ms ticks for the last 3 seconds (30 samples).
        horizon_s: how far ahead to forecast.
        sample_period_s: spacing between samples in price_buf_subsec.
        rho_clip: hard clamp on autocorrelation coefficient.
        decay: dampens the forecast magnitude (0.5 = 50%).

    Returns:
        Predicted price at now+horizon_s. Falls back to last price if the
        buffer is too short or autocorrelation can't be estimated.
    """
    n = len(price_buf_subsec) if price_buf_subsec else 0
    if n < 6:
        return price_buf_subsec[-1] if n else 0.0

    last = price_buf_subsec[-1]
    if last <= 0:
        return last

    # Compute log returns
    rets = []
    for i in range(1, n):
        prev, cur = price_buf_subsec[i - 1], price_buf_subsec[i]
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if len(rets) < 4:
        return last

    # Lag-1 autocorrelation
    mean = sum(rets) / len(rets)
    num = 0.0
    den = 0.0
    for i in range(len(rets)):
        d = rets[i] - mean
        den += d * d
        if i + 1 < len(rets):
            num += d * (rets[i + 1] - mean)
    rho = (num / den) if den > 0 else 0.0
    rho = max(-rho_clip, min(rho_clip, rho))

    # Forecast: drift = rho * last_return * (horizon / sample_period) * decay
    last_ret = rets[-1]
    n_steps = horizon_s / max(sample_period_s, 1e-6)
    drift = rho * last_ret * n_steps * decay

    return last * math.exp(drift)


# ─────────────────────────────────────────────────────────────────────────────
#  Helper: rolling buffer for fair-value computation
# ─────────────────────────────────────────────────────────────────────────────


class PriceBuffer1Hz:
    """Resamples sub-second price ticks into a 1-Hz buffer.

    Strategies need a 1-Hz history of BTC prices to compute realized vol over
    a 15-minute window. Binance ticks at variable rates (~5–50 ticks/sec).
    This buffer subsamples to one price per second using the last tick of
    each second.
    """

    def __init__(self, max_seconds: int = 1800) -> None:
        self._max = max_seconds
        self._prices: deque[float] = deque(maxlen=max_seconds)
        self._last_second: int = 0
        self._pending_price: float = 0.0

    def add_tick(self, ts: float, price: float) -> None:
        if price <= 0:
            return
        sec = int(ts)
        if self._last_second == 0:
            self._last_second = sec
            self._pending_price = price
            return
        if sec == self._last_second:
            self._pending_price = price  # update the pending second's price
        else:
            # Flush previous second's price; fill any gap with carry-forward
            gap = sec - self._last_second
            for _ in range(min(gap, self._max)):
                self._prices.append(self._pending_price)
            self._last_second = sec
            self._pending_price = price

    def snapshot(self) -> list[float]:
        # Include the pending second so callers see the freshest price
        out = list(self._prices)
        if self._pending_price > 0:
            out.append(self._pending_price)
        return out

    def __len__(self) -> int:
        return len(self._prices) + (1 if self._pending_price > 0 else 0)
