"""Unit tests for feature/detection math (offline, synthetic data)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.common import normalize as N  # noqa: E402
from src.features._util import cv, shannon_entropy  # noqa: E402
from src.features.pnl import settlement_pnl  # noqa: E402
from src.features.timing import timing_features  # noqa: E402


def test_parse_bucket_bounds():
    assert N.parse_bucket_bounds("54-55°F") == (54.0, 55.0)
    lo, hi = N.parse_bucket_bounds("53°F or below")
    assert lo == float("-inf") and hi == 53.0
    lo, hi = N.parse_bucket_bounds("84°F or above")
    assert lo == 84.0 and hi == float("inf")
    assert N.parse_bucket_bounds(None) is None


def test_entropy_and_cv():
    assert shannon_entropy([10, 0, 0]) == 0.0          # single bucket
    assert abs(shannon_entropy([5, 5]) - 1.0) < 1e-9    # 1 bit
    assert cv([5, 5, 5]) == 0.0                          # no variation
    assert cv([0, 10]) > 0


def test_settlement_pnl_buy_winner_and_loser():
    # BUY YES @0.6 (size10) on a market where YES wins -> +0.4*10 = +4
    # BUY YES @0.6 (size10) on a market where NO  wins -> -0.6*10 = -6
    trades = pd.DataFrame([
        {"proxy_wallet": "w", "condition_id": "A", "side": "BUY", "size": 10,
         "usdc": 6.0, "outcome_index": 0},
        {"proxy_wallet": "w", "condition_id": "B", "side": "BUY", "size": 10,
         "usdc": 6.0, "outcome_index": 0},
    ])
    markets = pd.DataFrame([
        {"condition_id": "A", "resolved": 1, "winning_outcome_index": 0},   # YES wins
        {"condition_id": "B", "resolved": 1, "winning_outcome_index": 1},   # NO wins
    ])
    out = settlement_pnl(trades, markets).iloc[0]
    assert abs(out["realized_pnl_usdc"] - (-2.0)) < 1e-6   # +4 -6 = -2
    assert out["n_trades_resolved"] == 2
    assert abs(out["fill_win_rate"] - 0.5) < 1e-9


def test_settlement_pnl_sell_leg():
    # SELL YES @0.6 (size10): cashflow +6; if YES wins terminal -10 -> -4
    trades = pd.DataFrame([{"proxy_wallet": "w", "condition_id": "A", "side": "SELL",
                            "size": 10, "usdc": 6.0, "outcome_index": 0}])
    markets = pd.DataFrame([{"condition_id": "A", "resolved": 1, "winning_outcome_index": 0}])
    out = settlement_pnl(trades, markets).iloc[0]
    assert abs(out["realized_pnl_usdc"] - (-4.0)) < 1e-6


def test_timing_features_detects_regular_machine():
    # perfectly regular 1s cadence across the clock -> low cv, high burst hours
    ts = list(range(1_776_000_000, 1_776_000_000 + 100))
    trades = pd.DataFrame({"proxy_wallet": ["bot"] * 100, "timestamp": ts,
                           "condition_id": ["m"] * 100})
    out = timing_features(trades).iloc[0]
    assert out["n_trades"] == 100
    assert out["cv_gap"] == 0.0           # constant 1s gaps
    assert out["median_gap_s"] == 1.0


def test_book_summary_best_levels_and_depth():
    from src.ingest.clob_live_capture import _summarise_book
    book = {
        "bids": [{"price": "0.50", "size": "100"}, {"price": "0.49", "size": "200"}],
        "asks": [{"price": "0.55", "size": "10"}, {"price": "0.52", "size": "20"}],
        "tick_size": "0.01", "last_trade_price": "0.51", "timestamp": "1780056693005",
        "hash": "abc",
    }
    s = _summarise_book(book)
    assert s["best_bid"] == 0.50 and s["best_ask"] == 0.52     # max bid / min ask
    assert s["best_bid_size"] == 100 and s["best_ask_size"] == 20
    assert s["mid"] == 0.51
    assert abs(s["bid_depth_usdc"] - (0.50 * 100 + 0.49 * 200)) < 1e-6
    assert s["n_bids"] == 2 and s["n_asks"] == 2
    assert s["_bids"][0][0] == 0.50 and s["_asks"][0][0] == 0.52  # sorted best-first


def test_book_summary_empty_side():
    from src.ingest.clob_live_capture import _summarise_book
    s = _summarise_book({"bids": [], "asks": [{"price": "0.001", "size": "5"}]})
    assert s["best_bid"] is None and s["mid"] is None and s["spread"] is None
    assert s["best_ask"] == 0.001


def test_bucket_center():
    from src.analyze.backtest_p1 import _bucket_center
    assert _bucket_center((54.0, 55.0)) == 54.5
    assert _bucket_center((float("-inf"), 53.0)) == 53.0   # 'or below'
    assert _bucket_center((84.0, float("inf"))) == 84.0     # 'or above'


def test_market_backtest_metrics_pnl():
    from src.analyze.backtest_p1_market import _metrics
    entries = [  # one winner, one loser, both entered at 0.8 with 10 shares
        {"entry_price": 0.8, "won": 1, "shares": 10, "notional": 8, "edge_share": 0.2},
        {"entry_price": 0.8, "won": 0, "shares": 10, "notional": 8, "edge_share": -0.8},
    ]
    m = _metrics(entries, {"taker_fee": 0.0, "taker_fee_stress": 0.02})
    assert m["n"] == 2 and m["hit_rate"] == 0.5
    assert m["deployed_usdc"] == 16
    assert m["pnl_fee0"] == -6          # 0.2*10 + (-0.8*10)
    assert abs(m["pnl_feeStress"] - (-6.4)) < 1e-6   # fee 0.02 * 20 shares = 0.4 extra cost


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} feature tests passed")
