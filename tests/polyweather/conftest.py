"""Shared pytest fixtures for the polyweather suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True, scope="session")
def _fixture_root_env() -> None:
    os.environ.setdefault(
        "POLYWEATHER_FIXTURE_ROOT",
        str(REPO_ROOT / "tests" / "fixtures" / "polyweather"),
    )


@pytest.fixture
def tmp_store(tmp_path: Path):
    from polybot.polyweather.persistence.store import PolyWeatherStore

    return PolyWeatherStore(tmp_path / "paper.sqlite")


@pytest.fixture
def resolver(tmp_path: Path):
    from polybot.polyweather.data.stations.station_resolver import StationResolver

    audit = tmp_path / "station_audit.jsonl"
    return StationResolver(audit_log_path=audit)


@pytest.fixture
def engine_factory(tmp_path: Path, resolver):
    from polybot.polyweather.orchestrator.engine import EngineConfig, PolyWeatherEngine
    from polybot.polyweather.persistence.store import PolyWeatherStore

    def _factory(duration: float = 2.0, cycle: float = 0.5):
        cfg = EngineConfig.from_files(
            risk_yaml=REPO_ROOT / "config" / "polyweather" / "risk.yaml",
            markets_yaml=REPO_ROOT / "config" / "polyweather" / "markets.yaml",
            weights_yaml=REPO_ROOT / "config" / "polyweather" / "strategy_weights.yaml",
            mode="paper",
            use_mock=True,
            cycle_seconds=cycle,
            duration_seconds=duration,
        )
        store = PolyWeatherStore(tmp_path / "paper.sqlite")
        engine = PolyWeatherEngine(cfg, store=store, station_resolver=resolver)
        return engine, store

    return _factory
