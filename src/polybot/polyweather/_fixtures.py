"""Resolve fixture paths consistently for every MockClient."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from polybot.polyweather import FIXTURE_ROOT_ENV


def fixture_root() -> Path:
    """Default to ``tests/fixtures/polyweather`` relative to repo root.

    Override with ``POLYWEATHER_FIXTURE_ROOT`` so tests can swap in their
    own deterministic data without touching shipped fixtures.
    """
    override = os.environ.get(FIXTURE_ROOT_ENV)
    if override:
        return Path(override).resolve()
    here = Path(__file__).resolve()
    # src/polybot/polyweather/_fixtures.py → repo root is 3 levels up
    repo_root = here.parents[3]
    return repo_root / "tests" / "fixtures" / "polyweather"


def load_fixture(name: str) -> Any:
    path = fixture_root() / name
    if not path.exists():
        raise FileNotFoundError(f"polyweather fixture missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))
