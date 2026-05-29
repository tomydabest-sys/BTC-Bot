"""Wipe data/runtime/polyweather/paper.sqlite (incl. WAL) and the audit log.

In WAL mode SQLite keeps recent writes in ``paper.sqlite-wal`` / ``-shm``
sidecar files. Deleting only ``paper.sqlite`` leaves those behind, so a
"reset" can resurrect stale equity/trade history on the next open — exactly
the kind of ghost data that makes the dashboard show a P&L disconnected from
the bankroll. We remove all three files plus the station audit log.

Usage:
  python scripts/polyweather/reset_paper_state.py        # interactive confirm
  python scripts/polyweather/reset_paper_state.py --yes  # non-interactive
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME = REPO_ROOT / "data" / "runtime" / "polyweather"
PAPER_DB = RUNTIME / "paper.sqlite"
AUDIT = RUNTIME / "station_audit.jsonl"


def _targets() -> list[Path]:
    # paper.sqlite + paper.sqlite-wal + paper.sqlite-shm + the audit log.
    files = [
        PAPER_DB,
        PAPER_DB.with_suffix(".sqlite-wal"),
        PAPER_DB.with_suffix(".sqlite-shm"),
        AUDIT,
    ]
    return [p for p in files if p.exists()]


def main() -> int:
    targets = _targets()
    if not targets:
        print("nothing to reset.")
        return 0
    non_interactive = "--yes" in sys.argv[1:] or "-y" in sys.argv[1:]
    if not non_interactive:
        listing = "\n  ".join(str(p) for p in targets)
        ans = input(f"delete these files?\n  {listing}\n[y/N]: ").strip().lower()
        if ans != "y":
            print("aborted.")
            return 1
    for p in targets:
        p.unlink()
        print("deleted", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
