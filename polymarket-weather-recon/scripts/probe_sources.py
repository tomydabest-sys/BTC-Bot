"""Phase 0 source verification — re-runnable.

Codifies the manual reconnaissance done during Phase 0: it probes every data
source the project intends to use, classifies each as reachable / blocked,
and writes a timestamped report to data/raw/_probes/. Run it any time to
re-confirm the live data landscape (e.g. after the network allowlist changes).

    python scripts/probe_sources.py

It deliberately uses raw `requests` (not the caching helper) so each run
reflects the CURRENT reachability rather than a cached verdict.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.common.config import load_config, project_root  # noqa: E402

WUNDERGROUND_NOTE = "blocked/scrape-only; Open-Meteo/NWS at same station = proxy"


def _probe(method: str, url: str, *, body: dict | None = None, headers: dict | None = None) -> dict:
    t0 = time.time()
    try:
        resp = requests.request(method, url, json=body, headers=headers, timeout=15)
        snippet = resp.text[:80].replace("\n", " ")
        return {
            "url": url,
            "status": resp.status_code,
            "ok": resp.status_code == 200,
            "ms": round((time.time() - t0) * 1000),
            "snippet": snippet,
        }
    except requests.RequestException as exc:
        return {"url": url, "status": None, "ok": False, "ms": round((time.time() - t0) * 1000), "snippet": str(exc)[:80]}


def main() -> int:
    cfg = load_config()
    probes: list[dict] = []

    g = cfg["sources"]["gamma"]["base_url"]
    probes.append({"name": "gamma", **_probe("GET", f"{g}/markets?limit=1&closed=false")})

    d = cfg["sources"]["data_api"]["base_url"]
    probes.append({"name": "data_api/trades", **_probe("GET", f"{d}/trades?limit=1")})

    c = cfg["sources"]["clob"]["base_url"]
    probes.append({"name": "clob", **_probe("GET", f"{c}/")})

    # Intended-but-blocked sources (expected to fail under the current allowlist)
    sg = cfg["sources"]["subgraph_goldsky"]
    probes.append({
        "name": "subgraph_goldsky(EXPECTED-BLOCKED)",
        **_probe("POST", sg["base_url"] + sg["candidate_endpoints"]["orderbook"],
                 body={"query": "{ _meta { block { number } } }"}),
    })
    for rpc in cfg["sources"]["onchain_polygon"]["rpc_urls"][:1]:
        probes.append({
            "name": "polygon_rpc(EXPECTED-BLOCKED)",
            **_probe("POST", rpc, body={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}),
        })

    om = cfg["weather_reference"]["open_meteo"]
    probes.append({"name": "open_meteo/forecast", **_probe(
        "GET", f"{om['forecast_url']}?latitude=40.78&longitude=-73.88&hourly=temperature_2m&past_days=1")})
    probes.append({"name": "open_meteo/archive(EXPECTED-BLOCKED)", **_probe(
        "GET", f"{om['archive_url']}?latitude=40&longitude=-74&start_date=2026-01-01&end_date=2026-01-02&daily=temperature_2m_max")})

    nws = cfg["weather_reference"]["nws"]
    probes.append({"name": "nws", **_probe(
        "GET", f"{nws['base_url']}/stations/KLGA/observations?limit=1",
        headers={"User-Agent": nws["user_agent"]})})

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = project_root() / "data" / "raw" / "_probes"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"phase0_probe_{ts}.json"
    out_path.write_text(json.dumps({"timestamp": ts, "probes": probes}, indent=2))

    print(f"{'SOURCE':<42} {'STATUS':>7}  {'MS':>6}  SNIPPET")
    print("-" * 90)
    for p in probes:
        status = p["status"] if p["status"] is not None else "ERR"
        flag = "OK " if p["ok"] else "BLK"
        print(f"{p['name']:<42} {str(status):>4} {flag}  {p['ms']:>5}  {p['snippet']}")
    print(f"\nwrote {out_path.relative_to(project_root())}")
    print(f"note: resolution source Wunderground is {WUNDERGROUND_NOTE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
