"""Config loading for the recon project.

Everything tunable lives in config.yaml; code reads it through here. Paths in
the config are resolved relative to the project root (the directory that
contains config.yaml) so the project is runnable from anywhere.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    """Directory containing config.yaml (two levels up from this file)."""
    return Path(__file__).resolve().parents[2]


@functools.lru_cache(maxsize=1)
def load_config(path: str | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else project_root() / "config.yaml"
    with cfg_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve_path(relative: str) -> Path:
    """Resolve a config-relative path (e.g. storage.sqlite_path) to absolute."""
    p = Path(relative)
    return p if p.is_absolute() else project_root() / p


def reachable_sources(cfg: dict[str, Any] | None = None) -> dict[str, bool]:
    """Map of source-name -> reachable flag, for quick guard checks."""
    cfg = cfg or load_config()
    out: dict[str, bool] = {}
    for name, spec in (cfg.get("sources") or {}).items():
        if isinstance(spec, dict) and "reachable" in spec:
            out[name] = bool(spec["reachable"])
    for name, spec in (cfg.get("weather_reference") or {}).items():
        if isinstance(spec, dict) and "reachable" in spec:
            out[f"weather:{name}"] = bool(spec["reachable"])
    return out
