"""P1 variant — FEED-DRIVEN resolution-drift backtest (Option-2).

The Open-Meteo trigger was killed (~35% bucket accuracy, +1.4°F warm bias) and
the market-price trigger does not generalise out-of-sample. This is the last
untested P1 angle: drive the trigger from a **real station feed** (IEM ASOS) —
the same observable Wunderground resolves on — and ask whether it gives an
information lead over the market on resolution day.

Two stages, run in order:

  (3c) ``feed_bucket_accuracy`` — the decisive gate. For each resolved event,
       take the IEM daily-max over the event's LOCAL day, map it to a bucket
       (unit-aware: °F ranges for US, °C single-degree for London/Paris), and
       compare to the actually-resolved winner. If overall accuracy is not
       clearly above the configured gate, the feed itself cannot pick the bucket
       and P1 is dead — we stop and report, no trading sim.

  (3d) ``feed_trading_backtest`` — only if the gate passes. A CAUSAL lock rule
       (no peek at the final high): walk the day's obs; once local clock has
       passed a lock hour AND temperature has fallen ``decline_margin_f`` below
       the running max for ``decline_readings`` readings, declare the high "in".
       The running-max bucket at lock = the feed's pick. Enter that bucket's YES
       at the first market print at/after the lock timestamp (trade-stream
       execution proxy — optimistic: no queue/slippage/impact). Hold to
       resolution; won = (feed_bucket == resolved winner) so early locks that get
       overtaken correctly count as losses. Sweep the lock hour; **fit on NYC,
       judge OUT-OF-SAMPLE.**

Decision rule: an edge exists only if ``avg_entry < feed_hit`` out-of-sample AND
post-fee OOS ROI is positive and robust across cities/lock-hours. If
``entry ≈ hit`` everywhere, the market already prices what the feed knows → no
edge.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..common import normalize as N
from ..common.config import load_config, project_root
from ..common.db import connect

log = logging.getLogger("recon.backtest_p1_feed")

US_CITIES = {"New York City", "Chicago", "Miami"}


# --------------------------------------------------------------------------- #
# Shared data assembly
# --------------------------------------------------------------------------- #
def _reference_local(conn, source: str, tz_map: dict) -> pd.DataFrame:
    ref = pd.read_sql_query(
        "SELECT station, ts, temp_f FROM reference_temp WHERE source=?", conn, params=(source,))
    if ref.empty:
        return ref
    ref["dt"] = pd.to_datetime(ref["ts"], unit="s", utc=True)
    parts = []
    for station, g in ref.groupby("station"):
        loc = g["dt"].dt.tz_convert(tz_map.get(station, "UTC"))
        parts.append(g.assign(local_date=loc.dt.strftime("%Y-%m-%d"),
                              local_hour=loc.dt.hour))
    return pd.concat(parts, ignore_index=True).sort_values("ts")


def _events(conn) -> dict:
    """resolved events -> per-event dict {city, station, target_date, winner,
    buckets:[(label,bounds)], by_label:{label:condition_id}}."""
    mk = pd.read_sql_query(
        "SELECT condition_id, event_slug, bucket_label, city, station, end_date, "
        "resolved, winning_outcome_index FROM markets WHERE station IS NOT NULL", conn)
    out = {}
    for slug, g in mk.groupby("event_slug"):
        win = g[(g["resolved"] == 1) & (g["winning_outcome_index"] == 0)]
        if win.empty:
            continue                                  # no resolved YES winner
        buckets = [(r["bucket_label"], N.parse_bucket_bounds(r["bucket_label"]))
                   for _, r in g.iterrows()]
        out[slug] = {
            "city": g["city"].iloc[0],
            "station": g["station"].iloc[0],
            "target_date": str(g["end_date"].iloc[0])[:10],
            "winner": win["bucket_label"].iloc[0],
            "buckets": buckets,
            "by_label": dict(zip(g["bucket_label"], g["condition_id"])),
        }
    return out


def _region(city: str) -> str:
    return "US" if city in US_CITIES else "intl"


# --------------------------------------------------------------------------- #
# (3c) accuracy gate
# --------------------------------------------------------------------------- #
def feed_bucket_accuracy(source: str | None = None) -> dict:
    cfg = load_config()
    source = source or cfg["backtest_p1_feed"]["source"]
    tz_map = {k: v.get("tz", "UTC") for k, v in cfg["weather_reference"]["stations"].items()}
    conn = connect()
    ref = _reference_local(conn, source, tz_map)
    events = _events(conn)
    conn.close()
    if ref.empty:
        return {"error": f"no reference_temp for source={source}; run IEM ingest first"}

    recs = []
    for slug, e in events.items():
        day = ref[(ref["station"] == e["station"]) & (ref["local_date"] == e["target_date"])]
        if day.empty:
            recs.append({"slug": slug, "city": e["city"], "covered": False})
            continue
        dmax = float(day["temp_f"].max())
        pick = N.temp_to_bucket(dmax, e["buckets"])
        recs.append({
            "slug": slug, "city": e["city"], "region": _region(e["city"]),
            "covered": True, "daily_max_f": round(dmax, 2),
            "feed_pick": pick, "winner": e["winner"],
            "correct": int(pick == e["winner"]),
        })
    df = pd.DataFrame(recs)
    cov = df[df["covered"]].copy()

    def acc(sub):
        return None if sub.empty else round(float(sub["correct"].mean()), 4)

    per_city = {c: {"n": int((cov["city"] == c).sum()),
                    "accuracy": acc(cov[cov["city"] == c])}
                for c in sorted(cov["city"].unique())}
    per_region = {r: {"n": int((cov["region"] == r).sum()),
                      "accuracy": acc(cov[cov["region"] == r])}
                  for r in sorted(cov["region"].unique())}
    overall_acc = acc(cov)
    gate_min = cfg["backtest_p1_feed"]["gate_min_accuracy"]
    return {
        "source": source,
        "n_events": int(df.shape[0]),
        "n_covered": int(cov.shape[0]),
        "overall_accuracy": overall_acc,
        "per_city": per_city,
        "per_region": per_region,
        "gate_min_accuracy": gate_min,
        "gate_passed": bool(overall_acc is not None and overall_acc > gate_min),
        "records": cov,            # for downstream diagnostics / report detail
    }


# --------------------------------------------------------------------------- #
# (3d) causal feed-driven trading backtest
# --------------------------------------------------------------------------- #
def _lock(day: pd.DataFrame, buckets, min_peak_hour: int, p) -> tuple[int | None, str | None, float | None]:
    """Causal lock: first ts where local clock past min_peak_hour AND temp has
    fallen decline_margin_f below the running max for decline_readings readings.
    Fallback: end-of-day. Returns (lock_ts, feed_bucket_label, locked_max)."""
    ts = day["ts"].to_numpy()
    temp = day["temp_f"].to_numpy()
    hour = day["local_hour"].to_numpy()
    if len(ts) == 0:
        return None, None, None
    run = -np.inf
    decl = 0
    for i in range(len(ts)):
        run = max(run, float(temp[i]))
        if temp[i] < run - p["decline_margin_f"]:
            decl += 1
        else:
            decl = 0
        if hour[i] >= min_peak_hour and decl >= p["decline_readings"]:
            return int(ts[i]), N.temp_to_bucket(run, buckets), run
    # never locked (monotone rise / no late decline) -> end-of-day fallback
    run = float(np.max(temp))
    return int(ts[-1]), N.temp_to_bucket(run, buckets), run


def _entry(fill, won: int, cap: float) -> dict:
    notional = min(float(fill["usdc"]), cap)
    price = float(fill["price"])
    return {"entry_price": price, "won": won, "notional": notional,
            "shares": notional / price if price > 0 else 0.0,
            "edge_share": (1.0 if won else 0.0) - price}


def _metrics(rows: list[dict], p) -> dict:
    if not rows:
        return {"n": 0}
    df = pd.DataFrame(rows)
    deployed = float(df["notional"].sum())
    def pnl(fee):
        return float(((df["edge_share"] - fee) * df["shares"]).sum())
    p0, pf = pnl(p["taker_fee"]), pnl(p["taker_fee_stress"])
    return {
        "n": int(len(df)),
        "feed_hit": round(float(df["won"].mean()), 4),
        "avg_entry": round(float(df["entry_price"].mean()), 4),
        "avg_edge_share": round(float(df["edge_share"].mean()), 4),
        "deployed_usdc": round(deployed, 0),
        "roi_fee0": round(p0 / deployed, 4) if deployed else None,
        "roi_feeStress": round(pf / deployed, 4) if deployed else None,
    }


def _run_lock_hour(events: dict, ref: pd.DataFrame, yes: pd.DataFrame,
                   min_peak_hour: int, p) -> dict:
    """Return per-group + per-city metrics for one lock-hour setting."""
    cap = p["per_entry_cap_usdc"]
    by_city_rows: dict[str, list] = {}
    for slug, e in events.items():
        day = ref[(ref["station"] == e["station"]) & (ref["local_date"] == e["target_date"])]
        if day.empty:
            continue
        lock_ts, feed_bucket, _ = _lock(day, e["buckets"], min_peak_hour, p)
        if feed_bucket is None:
            continue
        cid = e["by_label"].get(feed_bucket)
        if cid is None:
            continue
        fills = yes[(yes["condition_id"] == cid) & (yes["timestamp"] >= lock_ts)]
        if fills.empty:
            continue
        won = int(feed_bucket == e["winner"])
        by_city_rows.setdefault(e["city"], []).append(_entry(fills.iloc[0], won, cap))

    all_rows = [r for rows in by_city_rows.values() for r in rows]
    ins_rows = by_city_rows.get("New York City", [])
    oos_rows = [r for c, rows in by_city_rows.items() if c not in {"New York City"} for r in rows]
    return {
        "ALL": _metrics(all_rows, p),
        "in_sample_NYC": _metrics(ins_rows, p),
        "out_of_sample": _metrics(oos_rows, p),
        "per_city": {c: _metrics(rows, p) for c, rows in sorted(by_city_rows.items())},
    }


def feed_trading_backtest(source: str | None = None) -> dict:
    cfg = load_config()
    p = cfg["backtest_p1_feed"]
    source = source or p["source"]
    tz_map = {k: v.get("tz", "UTC") for k, v in cfg["weather_reference"]["stations"].items()}
    conn = connect()
    ref = _reference_local(conn, source, tz_map)
    events = _events(conn)
    trades = pd.read_sql_query(
        "SELECT condition_id, price, usdc, outcome_index, timestamp FROM trades "
        "WHERE outcome_index=0", conn)
    conn.close()
    if trades.empty:
        return {"error": "no trades; run Phase 2 ingest before the trading sim"}
    yes = trades.sort_values("timestamp")

    sweep = {h: _run_lock_hour(events, ref, yes, h, p) for h in p["lock_hours"]}
    return {"source": source, "by_lock_hour": sweep,
            "focus_lock_hour": p["focus_lock_hour"], "lock_hours": p["lock_hours"]}


# --------------------------------------------------------------------------- #
# Orchestration + report
# --------------------------------------------------------------------------- #
def run() -> dict:
    cfg = load_config()
    gate = feed_bucket_accuracy()
    if "error" in gate:
        return gate
    res = {"gate": gate}
    if gate["gate_passed"]:
        sim = feed_trading_backtest()
        res["sim"] = sim
    _write(res)
    return res


def _gate_lines(gate: dict) -> list[str]:
    lines = [
        "## (3c) Decisive gate — real-feed bucket accuracy (hindsight)", "",
        f"Per resolved event: IEM daily-max over the event's local day → bucket "
        f"(unit-aware) vs the resolved winner. Source = **{gate['source']}**. "
        f"Covered **{gate['n_covered']}/{gate['n_events']}** resolved events.", "",
        f"- **Overall accuracy: {gate['overall_accuracy']:.1%}** "
        f"(gate to clear: > {gate['gate_min_accuracy']:.0%} → "
        f"**{'PASS' if gate['gate_passed'] else 'FAIL'}**)",
        "",
        "| group | n | accuracy |", "|---|--:|--:|",
    ]
    for r, m in gate["per_region"].items():
        lines.append(f"| region:{r} | {m['n']} | {m['accuracy']:.1%} |")
    for c, m in gate["per_city"].items():
        lines.append(f"| {c} | {m['n']} | {m['accuracy']:.1%} |")
    lines.append("")
    lines.append("For reference, the Open-Meteo model proxy scored ~35% on the same task "
                 "(reports/backtest_p1.md). A real station feed clearing the gate is the "
                 "precondition for any feed-driven trade to have information value.")
    return lines


def _group_table(sweep: dict, group: str, lock_hours: list) -> list[str]:
    rows = [f"| lock hr | n | feed-hit | avg entry | edge/share | ROI(fee0) | ROI(fee 2%) |",
            "|--:|--:|--:|--:|--:|--:|--:|"]
    for h in lock_hours:
        m = sweep[h][group]
        if m.get("n", 0) == 0:
            rows.append(f"| {h} | 0 | — | — | — | — | — |")
            continue
        rows.append(f"| {h} | {m['n']} | {m['feed_hit']:.2f} | {m['avg_entry']:.3f} | "
                    f"{m['avg_edge_share']:+.4f} | {m['roi_fee0']:.3f} | {m['roi_feeStress']:.3f} |")
    return rows


def _verdict(sim: dict) -> list[str]:
    fh = sim["focus_lock_hour"]
    oos = sim["by_lock_hour"][fh]["out_of_sample"]
    # robustness: positive post-fee OOS ROI across ALL swept lock hours?
    oos_all = [sim["by_lock_hour"][h]["out_of_sample"] for h in sim["lock_hours"]]
    pos = [m for m in oos_all if m.get("n") and (m.get("roi_feeStress") or -9) > 0]
    robust = len(pos) == len([m for m in oos_all if m.get("n")])
    lines = ["## Verdict", ""]
    if not oos.get("n"):
        lines.append("- No out-of-sample entries fired — cannot judge an edge.")
        return lines
    entry_lt_hit = oos["avg_entry"] < oos["feed_hit"]
    edge = entry_lt_hit and (oos.get("roi_feeStress") or -9) > 0 and robust
    lines += [
        f"- Out-of-sample @ lock {fh}: feed-hit **{oos['feed_hit']:.2f}**, avg entry "
        f"**{oos['avg_entry']:.3f}**, ROI **{oos['roi_fee0']:.3f}** (fee0) / "
        f"**{oos['roi_feeStress']:.3f}** (fee 2%), n={oos['n']}.",
        f"- avg_entry < feed_hit out-of-sample? **{entry_lt_hit}** "
        f"(if entry ≈ hit, the market already prices what the feed knows).",
        f"- Post-fee OOS ROI positive at *every* swept lock hour? **{robust}**.",
        "",
        f"- **{'EDGE: a real, robust, post-fee out-of-sample edge exists.' if edge else 'NO deployable edge: the feed-driven P1 does not beat the market out-of-sample after fees.'}**",
    ]
    return lines


def _write(res: dict) -> str:
    gate = res["gate"]
    lines = [
        "# P1 variant — feed-driven resolution-drift backtest (Option-2)", "",
        "Trigger driven by a **real station feed (IEM ASOS)** instead of the Open-Meteo "
        "model. Unit-aware bucketing (°F ranges for US, °C single-degree for London/Paris). "
        "**NYC = in-sample; London/Paris/Chicago/Miami = OUT-OF-SAMPLE.**", "",
        *_gate_lines(gate), "",
    ]
    if not gate["gate_passed"]:
        lines += [
            "## Outcome", "",
            "- **Gate FAILED.** The feed cannot reliably pick the resolved bucket, so the "
            "feed-driven trading sim was not run — there is nothing for it to exploit.",
        ]
    elif "sim" in res and "error" in res["sim"]:
        lines += ["## (3d) Trading sim", "", f"- Not run: {res['sim']['error']}"]
    elif "sim" in res:
        sim = res["sim"]
        lines += [
            "## (3d) Causal feed-driven backtest", "",
            "Causal lock (no peek at the final high): once local clock ≥ lock-hour AND temp "
            "has fallen `decline_margin_f` below the running max for `decline_readings` "
            "readings, the running-max bucket is the feed's pick. Enter that bucket's YES at "
            "the first print at/after lock (trade-stream proxy — optimistic: no "
            "queue/slippage/impact); hold to resolution. Sweep the lock hour.", "",
            "### OUT-OF-SAMPLE (London + Paris + Chicago + Miami) — the real test", "",
            *_group_table(sim["by_lock_hour"], "out_of_sample", sim["lock_hours"]), "",
            "### In-sample (NYC) — for comparison", "",
            *_group_table(sim["by_lock_hour"], "in_sample_NYC", sim["lock_hours"]), "",
            f"### Per-city @ lock hour {sim['focus_lock_hour']}", "",
            "| city | n | feed-hit | avg entry | ROI(fee0) | ROI(fee 2%) |",
            "|---|--:|--:|--:|--:|--:|",
        ]
        pc = sim["by_lock_hour"][sim["focus_lock_hour"]]["per_city"]
        for c, m in pc.items():
            if m.get("n", 0) == 0:
                lines.append(f"| {c} | 0 | — | — | — | — |")
                continue
            lines.append(f"| {c} | {m['n']} | {m['feed_hit']:.2f} | {m['avg_entry']:.3f} | "
                         f"{m['roi_fee0']:.3f} | {m['roi_feeStress']:.3f} |")
        lines += ["", *_verdict(sim)]

    lines += [
        "", "## Caveats",
        "- Trade-stream proxy (no historical order book): assumes the print at/after lock is "
        "takeable with no queue/slippage and ignores our own market impact — **optimistic**.",
        "- IEM obs are ~hourly (US) / ~half-hourly (EU); a brief peak between obs can be "
        "missed, and the resolver may use a 6-hour max group we don't see → some gate misses "
        "are feed-granularity, not strategy, artefacts.",
        "- Hold-to-resolution PnL; per-entry capacity capped at "
        f"${load_config()['backtest_p1_feed']['per_entry_cap_usdc']}. ~60-day window per city.",
    ]
    out = project_root() / "reports" / "backtest_p1_feed.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)
