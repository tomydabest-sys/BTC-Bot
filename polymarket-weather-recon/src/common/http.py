"""Caching, backoff-aware HTTP helper.

Embodies three of the brief's operating principles:
  * cache raw responses aggressively  -> every response is written to data/raw/
    keyed by a hash of (method, url, params, body) before the caller sees it;
  * idempotent + resumable             -> re-running a request is a cache hit,
    so ingestion is safe to re-run and analysis is replayable offline;
  * respect rate limits                -> exponential backoff on network errors,
    429, and 5xx, plus a small polite delay between live calls.

Phase 0 only uses GET; POST is provided for the (currently blocked) GraphQL
subgraph so the same caching/backoff path is ready if the allowlist widens.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from .config import load_config, project_root

log = logging.getLogger("recon.http")


def _cache_key(method: str, url: str, params: Any, body: Any) -> str:
    payload = json.dumps(
        {"m": method.upper(), "u": url, "p": params, "b": body},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _cache_path(source: str, key: str) -> Path:
    d = project_root() / "data" / "raw" / source
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


def request_json(
    method: str,
    url: str,
    *,
    source: str,
    params: dict | None = None,
    body: dict | None = None,
    headers: dict | None = None,
    cache: bool = True,
    cache_bust: bool = False,
) -> Any:
    """Perform a GET/POST returning parsed JSON, with on-disk caching + backoff.

    `source` is a short label used as the cache subdirectory (e.g. "gamma").
    Set cache_bust=True to force a refetch and overwrite the cached copy.
    """
    cfg = load_config().get("ingestion", {}).get("http", {})
    timeout = cfg.get("timeout_seconds", 30)
    max_retries = cfg.get("max_retries", 4)
    backoff_base = cfg.get("backoff_base_seconds", 2)
    polite = cfg.get("polite_delay_seconds", 0.2)

    key = _cache_key(method, url, params, body)
    cpath = _cache_path(source, key)

    if cache and not cache_bust and cpath.exists():
        log.debug("cache hit %s %s -> %s", method, url, cpath.name)
        return json.loads(cpath.read_text())

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.request(
                method, url, params=params, json=body, headers=headers, timeout=timeout
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"retryable status {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            if cache:
                cpath.write_text(json.dumps(data))
            if polite:
                time.sleep(polite)
            return data
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            wait = backoff_base * (2 ** attempt)
            log.warning(
                "%s %s failed (attempt %d/%d): %s; retrying in %ss",
                method, url, attempt + 1, max_retries, exc, wait,
            )
            time.sleep(wait)
    raise RuntimeError(f"request failed after retries: {method} {url}: {last_exc}")


def get_json(url: str, *, source: str, **kw) -> Any:
    return request_json("GET", url, source=source, **kw)


def post_json(url: str, *, source: str, body: dict, **kw) -> Any:
    return request_json("POST", url, source=source, body=body, **kw)
