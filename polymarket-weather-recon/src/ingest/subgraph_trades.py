"""Phase 2 — INTENDED primary trade source (Goldsky subgraph). CURRENTLY BLOCKED.

The brief designates the Goldsky subgraph as the primary trade source because
it exposes BOTH maker and taker, fees, tx hash and log index per fill. During
Phase 0 every candidate endpoint under api.goldsky.com returned HTTP 403
"Host not in allowlist" (see FINDINGS.md). It is therefore unavailable under
the current network policy.

To enable: add `api.goldsky.com` to the environment network allowlist, then
re-verify the subgraph schema LIVE (entity/field names drift) before coding
against it. Candidate endpoints are recorded in config.yaml under
sources.subgraph_goldsky.candidate_endpoints.
"""
from __future__ import annotations

from ..common.config import reachable_sources


def ingest_subgraph_fills(*args, **kwargs):
    if not reachable_sources().get("subgraph_goldsky", False):
        raise RuntimeError(
            "Goldsky subgraph is blocked by the network allowlist "
            "('Host not in allowlist'). Add api.goldsky.com to the allowlist, "
            "set sources.subgraph_goldsky.reachable: true, re-verify the schema, "
            "then implement."
        )
    raise NotImplementedError("Phase 2 — implement once subgraph is reachable")
