"""Parse market rules text → canonical resolution station.

Audit-logged to ``data/runtime/polyweather/station_audit.jsonl`` so the
operator can manually verify each mapping (validation gate criterion #7
requires 100% verified).

Stations are NOT cached across runs — Polymarket has moved resolution
stations mid-cycle before. Always re-resolve per market session.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger()


@dataclass
class ResolvedStation:
    icao: str
    city: str
    source: str
    lat: float
    lon: float
    confidence: float
    matched_alias: str


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower())


class StationResolver:
    def __init__(
        self,
        catalog_path: Path | str | None = None,
        audit_log_path: Path | str | None = None,
        audit_enabled: bool = True,
    ) -> None:
        if catalog_path is None:
            catalog_path = (
                Path(__file__).resolve().parent.parent.parent
                / "data" / "stations" / "station_catalog.yaml"
            )
        catalog_path = Path(catalog_path)
        if not catalog_path.exists():
            # fallback to the alternative install layout
            alt = Path(__file__).resolve().parent / "station_catalog.yaml"
            if alt.exists():
                catalog_path = alt
        with catalog_path.open(encoding="utf-8") as fh:
            self._catalog: dict[str, dict[str, Any]] = yaml.safe_load(fh)["stations"]

        if audit_log_path is None:
            # __file__ = .../src/polybot/polyweather/data/stations/station_resolver.py
            # parents[5] = repo root
            audit_log_path = (
                Path(__file__).resolve().parents[5]
                / "data" / "runtime" / "polyweather" / "station_audit.jsonl"
            )
        self._audit_path = Path(audit_log_path)
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._audit_enabled = bool(audit_enabled)

    def resolve(self, market_id: str, rules_text: str) -> ResolvedStation | None:  # noqa: C901
        if not rules_text:
            self._log_unresolved(market_id, rules_text)
            return None
        text_norm = _normalise(rules_text)

        # 1. Direct ICAO match (KLGA, EGLC, etc.)
        for icao in self._catalog:
            if re.search(r"\b" + re.escape(icao.lower()) + r"\b", text_norm):
                station = self._build(icao, matched=icao, conf=1.0)
                self._audit(market_id, rules_text, station, "icao_direct")
                return station

        # 2. Alias match
        best: ResolvedStation | None = None
        for icao, info in self._catalog.items():
            for alias in info.get("aliases", []):
                alias_norm = _normalise(alias)
                if alias_norm and alias_norm in text_norm:
                    candidate = self._build(icao, matched=alias, conf=0.9)
                    if best is None or len(alias_norm) > len(best.matched_alias):
                        best = candidate
        if best is not None:
            self._audit(market_id, rules_text, best, "alias")
            return best

        # 3. City fallback
        for icao, info in self._catalog.items():
            city_norm = _normalise(info.get("city", ""))
            if city_norm and city_norm in text_norm:
                station = self._build(icao, matched=info["city"], conf=0.7)
                self._audit(market_id, rules_text, station, "city")
                return station

        self._log_unresolved(market_id, rules_text)
        return None

    def _build(self, icao: str, matched: str, conf: float) -> ResolvedStation:
        info = self._catalog[icao]
        return ResolvedStation(
            icao=icao,
            city=info.get("city", ""),
            source=info.get("source", ""),
            lat=float(info["lat"]),
            lon=float(info["lon"]),
            confidence=conf,
            matched_alias=matched,
        )

    def _log_unresolved(self, market_id: str, rules_text: str) -> None:
        logger.warning("station_unresolved", market_id=market_id, rules=rules_text[:120])
        self._audit_raw(
            {
                "market_id": market_id,
                "rules_excerpt": rules_text[:240],
                "resolved_station": None,
                "match_kind": "unresolved",
                "verified_by_operator": False,
                "ts": time.time(),
            }
        )

    def _audit(
        self,
        market_id: str,
        rules_text: str,
        station: ResolvedStation,
        match_kind: str,
    ) -> None:
        self._audit_raw(
            {
                "market_id": market_id,
                "rules_excerpt": rules_text[:240],
                "resolved_station": station.icao,
                "matched_alias": station.matched_alias,
                "confidence": station.confidence,
                "match_kind": match_kind,
                "verified_by_operator": False,
                "ts": time.time(),
            }
        )

    def _audit_raw(self, payload: dict[str, Any]) -> None:
        # Mock mode disables auditing to keep the file from growing across
        # every fixture-driven cycle (the user reported 1326 "verified" entries
        # accumulating in a single demo session).
        if not self._audit_enabled:
            return
        try:
            with self._audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload) + "\n")
        except OSError as exc:
            logger.warning("audit_write_failed", error=str(exc))

    # ─── audit log readers ────────────────────────────────────────────

    def read_audit(self) -> list[dict[str, Any]]:
        if not self._audit_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self._audit_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries

    def accuracy_stats(self) -> dict[str, Any]:
        rows = self.read_audit()
        if not rows:
            return {"total": 0, "correct": 0, "accuracy": 0.0, "unresolved": 0}
        resolved = [r for r in rows if r.get("resolved_station")]
        verified = [r for r in resolved if r.get("verified_by_operator")]
        unresolved = sum(1 for r in rows if not r.get("resolved_station"))
        # Per the prompt: accuracy = verified / total, validation gate wants 100%
        total = len(rows)
        accuracy = (len(verified) / total) if total else 0.0
        return {
            "total": total,
            "resolved": len(resolved),
            "verified": len(verified),
            "unresolved": unresolved,
            "correct": len(verified),
            "accuracy": accuracy,
        }
