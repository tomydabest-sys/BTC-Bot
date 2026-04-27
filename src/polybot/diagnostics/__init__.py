"""Diagnostics package — decision log, block-reason counter, health monitor.

The decision log is the highest-leverage diagnostic in the bot. Every strategy
evaluate() call ends with emit() so logs/decisions.jsonl carries one canonical
row per (strategy, market, cycle) — including non-trades.
"""

from polybot.diagnostics import decision_log

__all__ = ["decision_log"]
