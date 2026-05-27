"""Wipe data/runtime/polyweather/paper.sqlite and the decisions log."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PAPER_DB = REPO_ROOT / "data" / "runtime" / "polyweather" / "paper.sqlite"
AUDIT = REPO_ROOT / "data" / "runtime" / "polyweather" / "station_audit.jsonl"


def main() -> int:
    if not PAPER_DB.exists() and not AUDIT.exists():
        print("nothing to reset.")
        return 0
    ans = input(f"delete {PAPER_DB} and {AUDIT}? [y/N]: ").strip().lower()
    if ans != "y":
        print("aborted.")
        return 1
    for p in (PAPER_DB, AUDIT):
        if p.exists():
            p.unlink()
            print("deleted", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
