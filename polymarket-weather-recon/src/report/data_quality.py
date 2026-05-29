"""Phase 2 data-quality report.

Validates the ingested `trades` table and writes reports/data_quality_phase2.md.
Honest about the structural limits surfaced in Phase 0 (taker-side only; possible
per-market truncation at the 250/page boundary).
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..common.config import project_root
from ..common.db import connect


def compute(conn=None) -> dict:
    own = conn is None
    conn = conn or connect()
    q = conn.execute

    n_markets = q("SELECT COUNT(*) c FROM markets").fetchone()["c"]
    n_trades = q("SELECT COUNT(*) c FROM trades").fetchone()["c"]
    volume = q("SELECT COALESCE(SUM(usdc),0) v FROM trades").fetchone()["v"]
    wallets = q("SELECT COUNT(DISTINCT proxy_wallet) c FROM trades").fetchone()["c"]
    cov = q("SELECT MIN(ts_iso) lo, MAX(ts_iso) hi FROM trades").fetchone()
    sides = {r["side"]: r["c"] for r in q(
        "SELECT side, COUNT(*) c FROM trades GROUP BY side").fetchall()}
    mkts_with = q("SELECT COUNT(DISTINCT condition_id) c FROM trades").fetchone()["c"]
    price_rng = q("SELECT MIN(price) lo, MAX(price) hi FROM trades").fetchone()
    # possible truncation: markets whose ingested count is an exact multiple of 250
    trunc = [dict(r) for r in q(
        """SELECT condition_id, rows_ingested FROM ingest_log
           WHERE rows_ingested>0 AND rows_ingested % 250 = 0""").fetchall()]
    top = [dict(r) for r in q(
        """SELECT t.condition_id, m.question, COUNT(*) trades, ROUND(SUM(t.usdc),2) usdc
           FROM trades t LEFT JOIN markets m ON m.condition_id=t.condition_id
           GROUP BY t.condition_id ORDER BY trades DESC LIMIT 10""").fetchall()]
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "markets_discovered": n_markets,
        "markets_with_trades": mkts_with,
        "markets_zero_trades": n_markets - mkts_with,
        "trades": n_trades,
        "usdc_volume": round(volume, 2),
        "unique_wallets": wallets,
        "date_min": cov["lo"], "date_max": cov["hi"],
        "side_split": sides,
        "price_min": price_rng["lo"], "price_max": price_rng["hi"],
        "possible_truncation_markets": trunc,
        "top_markets": top,
    }
    if own:
        conn.close()
    return out


def write_markdown(stats: dict) -> str:
    out = project_root() / "reports" / "data_quality_phase2.md"
    lines = [
        "# Phase 2 — data-quality report (bounded window)",
        "",
        f"Generated: {stats['generated_at']}",
        "",
        "## Coverage",
        f"- Markets discovered: **{stats['markets_discovered']}**",
        f"- Markets with ≥1 trade: **{stats['markets_with_trades']}** "
        f"(zero-trade: {stats['markets_zero_trades']})",
        f"- Trades ingested: **{stats['trades']}**",
        f"- USDC volume (Σ size×price): **{stats['usdc_volume']:,}**",
        f"- Unique wallets (taker-side): **{stats['unique_wallets']}**",
        f"- Date range: {stats['date_min']} → {stats['date_max']}",
        f"- Price range observed: {stats['price_min']} – {stats['price_max']} "
        "(sanity: must be within [0,1])",
        f"- Side split: {stats['side_split']}",
        "",
        "## Honesty / known limits",
        "- **Taker-side only.** The Data API exposes one wallet per fill; the "
        "maker counterparty is not available (subgraph/on-chain blocked). "
        "`side_split` is taker BUY/SELL, NOT maker-vs-taker.",
        "- **No on-chain reconciliation** under the current allowlist; "
        "completeness is argued from internal consistency only.",
        "",
        "## Possible truncation (verify)",
        "Markets whose ingested count is an exact multiple of 250 (the page cap) "
        "may be truncated by the Data API's pagination limit:",
    ]
    if stats["possible_truncation_markets"]:
        for m in stats["possible_truncation_markets"]:
            lines.append(f"- `{m['condition_id']}` — {m['rows_ingested']} rows")
    else:
        lines.append("- none — no market hit an exact 250-multiple boundary.")
    lines += ["", "## Top markets by trade count", "",
              "| trades | usdc | question |", "|--:|--:|---|"]
    for m in stats["top_markets"]:
        lines.append(f"| {m['trades']} | {m['usdc']} | {m['question']} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)
