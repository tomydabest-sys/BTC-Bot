"""Forward-validation of the reference signal (Option-2).

Quantifies how well each reference source's daily-max identifies the resolved
winning bucket, and ACCUMULATES that evidence over time. NWS METAR has only ~2
days of history, so this is designed to be run daily (cron) — each run appends
the latest resolved days into `reference_validation`, building the sample the
30-day backtest cannot (NWS has no deep history).

Compares Open-Meteo vs NWS side by side against the actual winner.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from ..common import normalize as N
from ..common.config import load_config, project_root
from ..common.db import connect

log = logging.getLogger("recon.refval")


def _bucket_for(value: float, buckets: list[tuple[str, tuple]]) -> str | None:
    for label, (lo, hi) in buckets:
        if lo <= value <= hi:
            return label
    return None


def validate(conn=None) -> dict:
    own = conn is None
    conn = conn or connect()
    cfg = load_config()
    tz_map = {k: v.get("tz", "UTC") for k, v in cfg["weather_reference"]["stations"].items()}

    # ALL discovered markets (resolved or not) — we need buckets + resolution state.
    mk = pd.read_sql_query(
        "SELECT event_slug, bucket_label, station, end_date, resolved, winning_outcome_index "
        "FROM markets WHERE station IS NOT NULL", conn)
    ref = pd.read_sql_query("SELECT source, station, ts, temp_f FROM reference_temp", conn)
    if ref.empty or mk.empty:
        if own:
            conn.close()
        return {"note": "no reference or markets yet"}
    ref["dt"] = pd.to_datetime(ref["ts"], unit="s", utc=True)

    captured_at = datetime.now(timezone.utc).isoformat()
    logged = 0
    with conn:
        # (a) capture: for every (station,date) the reference covers, record the
        #     implied bucket; fill the match now if the event is already resolved,
        #     else leave pending (NULL) to be backfilled on a later run.
        for slug, g in mk.groupby("event_slug"):
            station = g["station"].iloc[0]
            target_date = str(g["end_date"].iloc[0])[:10]
            buckets = [(r["bucket_label"], N.parse_bucket_bounds(r["bucket_label"]))
                       for _, r in g.iterrows() if N.parse_bucket_bounds(r["bucket_label"])]
            if not buckets:
                continue
            win = g[(g["resolved"] == 1) & (g["winning_outcome_index"] == 0)]
            actual = win["bucket_label"].iloc[0] if not win.empty else None
            tz = tz_map.get(station, "UTC")
            for source in ("open_meteo", "nws"):
                sref = ref[(ref["source"] == source) & (ref["station"] == station)]
                if sref.empty:
                    continue
                loc_date = sref["dt"].dt.tz_convert(tz).dt.strftime("%Y-%m-%d")
                day = sref[loc_date == target_date]
                if day.empty:
                    continue
                ref_max = round(float(day["temp_f"].max()), 1)
                # the resolver reports the daily high as a WHOLE degree °F, so map
                # the reference to a bucket via the rounded integer.
                rb = _bucket_for(round(ref_max), buckets)
                matched = int(rb == actual) if actual is not None else None
                conn.execute(
                    """INSERT OR REPLACE INTO reference_validation
                       (captured_at,station,target_date,source,ref_max_f,ref_bucket,
                        actual_winner,matched) VALUES (?,?,?,?,?,?,?,?)""",
                    (captured_at, station, target_date, source, ref_max, rb, actual, matched))
                logged += 1

        # (b) backfill: any earlier pending row whose event has since resolved.
        winners = {(r["station"], str(r["end_date"])[:10]): r["bucket_label"]
                   for _, r in mk[(mk["resolved"] == 1) &
                                  (mk["winning_outcome_index"] == 0)].iterrows()}
        for r in conn.execute(
                "SELECT station,target_date,source,ref_bucket FROM reference_validation "
                "WHERE matched IS NULL").fetchall():
            actual = winners.get((r["station"], r["target_date"]))
            if actual is not None:
                conn.execute(
                    "UPDATE reference_validation SET actual_winner=?, matched=? "
                    "WHERE station=? AND target_date=? AND source=?",
                    (actual, int(r["ref_bucket"] == actual), r["station"],
                     r["target_date"], r["source"]))

    rows = pd.read_sql_query("SELECT * FROM reference_validation", conn)
    summary = {}
    for source, gs in rows.groupby("source"):
        scored = gs[gs["matched"].notna()]
        summary[source] = {
            "scored_days": int(len(scored)),
            "pending_days": int(gs["matched"].isna().sum()),
            "match_rate": round(float(scored["matched"].mean()), 4) if len(scored) else None,
            "mean_signed_bias_f": _bias(scored) if len(scored) else None,
        }
    if own:
        conn.close()
    return {"logged_this_run": logged, "by_source": summary, "rows": rows}


def _bias(gs: pd.DataFrame) -> float | None:
    biases = []
    for _, r in gs.iterrows():
        b = N.parse_bucket_bounds(r["actual_winner"])
        if not b:
            continue
        lo, hi = b
        center = hi if lo == float("-inf") else (lo if hi == float("inf") else (lo + hi) / 2)
        biases.append(r["ref_max_f"] - center)
    return round(float(np.mean(biases)), 2) if biases else None


def write_report(res: dict) -> str:
    out = project_root() / "reports" / "reference_validation.md"
    lines = ["# Reference validation — NWS vs Open-Meteo (forward-accumulating)", "",
             "How often each reference source's daily-max lands in the **resolved winning "
             "bucket** (2°F wide). NWS METAR has ~2-day retention, so this table GROWS each "
             "time the harness is run (designed for a daily cron).", "",
             "| source | scored days | pending | match rate | mean signed bias (°F) |",
             "|---|--:|--:|--:|--:|"]
    for src, s in res.get("by_source", {}).items():
        mr = f"{s['match_rate']:.0%}" if s.get("match_rate") is not None else "—"
        lines.append(f"| {src} | {s['scored_days']} | {s['pending_days']} | {mr} | "
                     f"{s.get('mean_signed_bias_f')} |")
    om = res.get("by_source", {}).get("open_meteo", {})
    om_mr = f"{om.get('match_rate'):.0%}" if om.get("match_rate") is not None else "n/a"
    lines += ["", "_Scored = compared against a resolved winner. Pending = reference captured, "
              "awaiting resolution (NWS only reaches the unresolved present, so it scores "
              "forward — re-run daily). Reference daily-max is rounded to a whole °F before "
              "bucket assignment, matching how the resolver reports the high._", "",
              f"**Interpretation:** even with correct whole-degree rounding, Open-Meteo matches "
              f"the 2°F winning bucket only **{om_mr}** of the time, dragged down by a systematic "
              f"**+{om.get('mean_signed_bias_f')}°F** warm bias vs the station resolver — it "
              "lands one bucket too high. NWS METAR is the actual station class used to resolve, "
              "so it should match far better; it can only be scored AFTER each day resolves, so "
              "re-run this daily to accumulate NWS evidence before trusting NWS for live P1.", ""]
    if isinstance(res.get("rows"), pd.DataFrame) and not res["rows"].empty:
        recent = res["rows"].sort_values("target_date").tail(16)
        lines += ["## Recent comparisons", "",
                  "| date | source | ref_max | ref_bucket | actual_winner | result |",
                  "|---|---|--:|---|---|:--:|"]
        for _, r in recent.iterrows():
            mark = "pending" if pd.isna(r["matched"]) else ("✓" if r["matched"] else "✗")
            lines.append(f"| {r['target_date']} | {r['source']} | {r['ref_max_f']} | "
                         f"{r['ref_bucket']} | {r['actual_winner'] if pd.notna(r['actual_winner']) else '—'} | "
                         f"{mark} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)
