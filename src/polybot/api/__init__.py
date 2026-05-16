"""CLOB V2 API layer — rate limiting, signed client wrapper, secrets.

This package is the boundary between the bot and Polymarket's V2 REST/WS
surface. It deliberately does NOT import any V1 SDK symbols. Live trading
is still gated by `polybot.data.client.LIVE_TRADING_ENABLED`; until that
flag flips, the modules here are exercised only via paper-mode harnesses
and unit tests.
"""

from __future__ import annotations
