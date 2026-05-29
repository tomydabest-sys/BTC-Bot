"""Phase 2 — INTENDED on-chain fallback / verification. CURRENTLY BLOCKED.

Decode CTF Exchange + NegRisk CTF Exchange OrderFilled / OrdersMatched events
on Polygon to (a) verify subgraph/Data-API completeness and (b) patch gaps.

During Phase 0, ALL tested Polygon RPC providers and api.polygonscan.com
returned HTTP 403 "Host not in allowlist" (see FINDINGS.md). On-chain ingestion
is therefore unavailable under the current network policy.

Note: contract ABIs themselves CAN be fetched (raw.githubusercontent.com is
reachable), but decoding still requires an allowlisted RPC. Contract addresses
in config.yaml are well-known but UNVERIFIED — re-confirm against Polygonscan/
Polymarket docs before trusting any decode.

To enable: add a Polygon RPC host to the allowlist, set
sources.onchain_polygon.reachable: true, install web3, verify addresses+ABIs.
"""
from __future__ import annotations

from ..common.config import reachable_sources


def ingest_onchain_fills(*args, **kwargs):
    if not reachable_sources().get("onchain_polygon", False):
        raise RuntimeError(
            "Polygon RPC / Polygonscan are blocked by the network allowlist "
            "('Host not in allowlist'). Add an RPC host, set "
            "sources.onchain_polygon.reachable: true, then implement."
        )
    raise NotImplementedError("Phase 2 — implement once a Polygon RPC is reachable")
