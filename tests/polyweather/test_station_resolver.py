"""Station resolver against 30 fixture rules-text samples."""

from __future__ import annotations

import json
from pathlib import Path

from polybot.polyweather.data.stations.station_resolver import StationResolver

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures" / "polyweather" / "market_rules_text_samples.json"
)


def test_resolves_all_thirty_samples(tmp_path):
    samples = json.loads(FIXTURE.read_text(encoding="utf-8"))["samples"]
    resolver = StationResolver(audit_log_path=tmp_path / "audit.jsonl")
    misses: list[str] = []
    for s in samples:
        result = resolver.resolve(s["market_id"], s["rules"])
        expected = s["expected_station"]
        actual = result.icao if result else None
        if actual != expected:
            misses.append(f"{s['market_id']}: expected {expected}, got {actual}")
    assert not misses, "\n".join(misses)


def test_unresolvable_returns_none(tmp_path):
    resolver = StationResolver(audit_log_path=tmp_path / "audit.jsonl")
    out = resolver.resolve("m_x", "some city in space with no known weather station")
    assert out is None


def test_audit_log_written(tmp_path):
    audit = tmp_path / "audit.jsonl"
    resolver = StationResolver(audit_log_path=audit)
    resolver.resolve("m_klga", "Reading at LaGuardia (KLGA)")
    assert audit.exists()
    contents = audit.read_text(encoding="utf-8").strip().split("\n")
    assert len(contents) == 1
    parsed = json.loads(contents[0])
    assert parsed["resolved_station"] == "KLGA"
    assert parsed["verified_by_operator"] is False
