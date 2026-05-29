"""Phase 7 — BTC-Bot recommendations (PROPOSALS ONLY).

Maps the recon findings to concrete, prioritised proposals for BTC-Bot, each
with evidence, expected effect, implementation sketch and risk/assumptions. Also
emits reports/insights.json — a machine-readable artifact BTC-Bot could later
consume (calibrated numbers only).

HARD CONSTRAINT: read-only w.r.t. BTC-Bot. This module imports nothing from and
writes nothing into BTC-Bot. It only produces markdown + JSON for review.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from ..common.config import project_root
from ..common.db import connect


def _calibration() -> dict:
    conn = connect()
    feats = pd.read_sql_query("SELECT * FROM wallet_features", conn)
    cls = pd.read_sql_query(
        "SELECT proxy_wallet, bot_score FROM wallet_classification", conn)
    conn.close()
    bots = feats.merge(cls, on="proxy_wallet").query("bot_score >= 0.5")
    strong = bots.query("bot_score >= 0.7")

    def med(col, frame=bots):
        return round(float(frame[col].median()), 4) if col in frame and len(frame) else None

    return {
        "n_bots_ge_0p5": int(len(bots)),
        "n_bots_ge_0p7": int(len(strong)),
        "median_bot_active_hours": med("active_hours"),
        "median_bot_size_shares": med("median_size"),
        "median_bot_breadth_markets": med("breadth_markets"),
        "median_bot_max_trades_per_sec": med("max_trades_per_sec"),
        "median_bot_fill_win_rate": med("fill_win_rate"),
        "median_bot_roi_on_resolved": med("roi_on_resolved"),
        "strong_bot_median_breadth": med("breadth_markets", strong),
        "strong_bot_median_active_hours": med("active_hours", strong),
    }


def build_recommendations() -> tuple[str, str]:
    cal = _calibration()
    insights = {
        "source": "polymarket-weather-recon, bounded NYC-temperature/30-day window",
        "disclaimer": "behavioural inference from taker-side data; revalidate out-of-sample",
        "calibration": cal,
        "constraints": {
            "maker_taker_pairing": "unavailable (subgraph/on-chain blocked)",
            "reference_granularity": "hourly (Open-Meteo); not sub-second",
            "history_reach_days": 90,
        },
    }
    out_json = project_root() / "reports" / "insights.json"
    out_json.write_text(json.dumps(insights, indent=2), encoding="utf-8")

    def g(k):
        v = cal.get(k)
        return "n/a" if v is None else v

    lines = [
        "# Phase 7 — BTC-Bot recommendations (PROPOSALS ONLY)", "",
        "> Read-only analysis. **Nothing here is implemented in BTC-Bot.** Each "
        "proposal lists evidence, expected effect, an implementation sketch, and "
        "risk/assumptions. Machine-readable calibration is in `reports/insights.json`.",
        "", "## Calibration snapshot (from the observed bot population)",
        f"- Bots (score≥0.5): **{g('n_bots_ge_0p5')}**, strong (≥0.7): **{g('n_bots_ge_0p7')}**",
        f"- Median bot: **{g('median_bot_active_hours')}/24** active hours, "
        f"breadth **{g('median_bot_breadth_markets')}** markets, median size "
        f"**{g('median_bot_size_shares')}** shares, up to **{g('median_bot_max_trades_per_sec')}** "
        f"fills/s, win rate **{g('median_bot_fill_win_rate')}**, ROI **{g('median_bot_roi_on_resolved')}**.",
        "",
        "## P1 — Resolution-drift capture (highest priority)",
        "- **Evidence:** dossiers + exploits.md H1 — dedicated snipers harvest the "
        "convergence of the winning bucket's YES to $1 on the resolution day "
        "(op_004: 99% resolution-day trades, win 0.91, thin ROI).",
        "- **Proposal:** add a resolution-drift strategy that, once the reference "
        "running-max temperature has entered a bucket and the day is past its peak, "
        "buys the residual gap to $1 (and sells exceeded buckets toward $0).",
        "- **Expected effect:** high-hit-rate, thin-edge flow — many small wins, the "
        "dominant profitable pattern observed.",
        "- **Implementation sketch:** consume `insights.json` + a station temperature "
        "feed; gate entries on (running_max in-bucket) AND (local time > climatological "
        "peak hour); size small; hold to resolution.",
        "- **Risk/assumptions:** mis-timing before the daily peak flips winners to "
        "losers; reference latency vs the existing fast snipers; capacity is bounded.",
        "",
        "## P2 — Fee/rebate-aware, fixed-size maker quoting",
        "- **Evidence:** top bots are high-breadth, fixed-ish small sizes, ~0.88-0.94 "
        "win rate, thin positive ROI ⇒ spread/fair-value capture, not directional bets.",
        "- **Proposal:** maker-quoting with small fixed notional per bucket and "
        "rebate-aware placement (Polymarket rewards/maker fee schedule from Gamma "
        "`makerBaseFee`/rewards tags).",
        f"- **Implementation sketch:** quote size ≈ observed median (~{g('median_bot_size_shares')} "
        "shares); breadth across buckets; tick-offset from fair value at the 0.001 tick.",
        "- **Risk/assumptions:** adverse selection (unmeasured here — needs Phase 8 "
        "live capture); rebate economics can flip with fee changes.",
        "",
        "## P3 — Latency / cadence target",
        f"- **Evidence:** fastest bots reach multiple fills/second and are "
        f"{g('strong_bot_median_active_hours')}/24 active.",
        "- **Proposal:** set a polling/reaction budget of **sub-second on the order path "
        "and sub-minute on the reference** to compete for P1; do not chase pure latency "
        "arb (observed to be largely unprofitable post dynamic fees).",
        "- **Risk/assumptions:** infra cost; diminishing returns past 'good enough'.",
        "",
        "## P4 — Market-selection filter",
        f"- **Evidence:** {g('median_bot_breadth_markets')}-market breadth among bots, but "
        "exploits.md H3 shows many <20-trade tail buckets; H2 shows thin off-peak hours.",
        "- **Proposal:** prioritise liquid central buckets on the resolution day; treat "
        "tail buckets only as small resolution-short candidates; avoid quoting in the "
        "thinnest UTC hours unless pick-off spreads justify it.",
        "- **Risk/assumptions:** concentration risk; thin-hour edges may not exist "
        "until verified on live-book data.",
        "",
        "## P5 — Sizing model",
        "- **Evidence:** bot sizes are small and often fixed (mode-size fractions high "
        "for ladder/grid archetypes); ROI is thin and win-rate-driven.",
        "- **Proposal:** small fixed/tiered sizing per bucket rather than aggressive "
        "Kelly on long-shot edges (echoes BTC-Bot's prior tail-price blow-up bug); cap "
        "per-bucket and per-event exposure.",
        "- **Risk/assumptions:** under-sizing leaves edge on the table; calibrate to "
        "bankroll.",
        "",
        "## Insight artifact", "`reports/insights.json` carries the calibrated numbers "
        "(active hours, sizes, breadth, win rates, ROI bands, constraints) for BTC-Bot "
        "to consume programmatically.",
        "",
        "## Which proposals to take forward?",
        "Please tell me which of **P1–P5** to develop into a detailed spec (still as a "
        "proposal — no BTC-Bot code changes without your go-ahead), and whether to widen "
        "the allowlist / scale the sample first to harden the evidence.",
    ]
    out_md = project_root() / "reports" / "btc_bot_recommendations.md"
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out_md), str(out_json)
