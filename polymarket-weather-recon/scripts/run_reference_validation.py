"""Reference-validation driver (Option-2): fetch NWS, validate vs resolver.

    python scripts/run_reference_validation.py        # run daily to accumulate evidence

Fetches the latest NWS observations (also ensures Open-Meteo exists), then logs
NWS-vs-Open-Meteo daily-max accuracy against resolved winners into
reference_validation and writes reports/reference_validation.md.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ingest.reference_feed import build_nws_reference, build_reference_for_window  # noqa: E402
from src.analyze.reference_validation import validate, write_report  # noqa: E402
from src.common.db import connect  # noqa: E402


def main() -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s %(message)s")
    # ensure Open-Meteo exists (idempotent/cached) and refresh NWS (live)
    conn = connect()
    have_om = conn.execute(
        "SELECT COUNT(*) c FROM sqlite_master WHERE name='reference_temp'").fetchone()[0]
    conn.close()
    if have_om:
        pass
    print("NWS:", build_nws_reference(cache_bust=True))
    try:
        build_reference_for_window()  # tops up Open-Meteo if missing (cached)
    except Exception as exc:
        print("open-meteo refresh skipped:", exc)

    res = validate()
    print(json.dumps(res.get("by_source", res), indent=2, default=str))
    print("logged this run:", res.get("logged_this_run"))
    print("wrote", write_report(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
