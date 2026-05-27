"""Interactive CLI to walk through unverified station mappings."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from polybot.polyweather.data.stations.station_resolver import StationResolver


def main() -> int:
    resolver = StationResolver()
    rows = resolver.read_audit()
    if not rows:
        print("no audit entries — run discover_markets.py first.")
        return 0
    unverified = [r for r in rows if not r.get("verified_by_operator")]
    if not unverified:
        print("all entries verified.")
        return 0
    print(f"{len(unverified)} unverified entries.")
    audit_path: Path = resolver._audit_path  # type: ignore[attr-defined]
    out_lines: list[str] = []
    for r in rows:
        if r.get("verified_by_operator"):
            out_lines.append(json.dumps(r))
            continue
        print("───")
        print(f"market_id: {r.get('market_id')}")
        print(f"rules: {r.get('rules_excerpt')}")
        print(f"resolved: {r.get('resolved_station')}  match_kind={r.get('match_kind')}")
        ans = input("[y]es / [n]o / [s]kip: ").strip().lower() or "s"
        if ans == "y":
            r["verified_by_operator"] = True
        elif ans == "n":
            override = input("Override ICAO (or blank to mark unresolved): ").strip()
            r["resolved_station"] = override or None
            r["verified_by_operator"] = bool(override)
        out_lines.append(json.dumps(r))
    audit_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print("audit log updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
