"""Secrets helpers — keyring with environment-variable fallback.

The brief flags plaintext private keys in `.env` as a common failure mode.
Order of resolution for `get_secret(name)`:

  1. OS environment variable `name` (production / CI / docker secrets).
  2. `keyring` system store under service "btc-bot" (developer-local).
  3. None.

`keyring` is an *optional* dependency — if it isn't installed we silently
fall through to env-var only. This keeps the package usable in minimal
Docker images while encouraging a safer pattern locally.
"""

from __future__ import annotations

import os

try:  # pragma: no cover - import-time conditional
    import keyring  # type: ignore[import-not-found]
    _KEYRING_AVAILABLE = True
except Exception:  # pragma: no cover
    keyring = None  # type: ignore[assignment]
    _KEYRING_AVAILABLE = False


SERVICE_NAME = "btc-bot"


def get_secret(name: str, *, default: str | None = None) -> str | None:
    """Resolve a secret by name. Returns None when neither source has it."""
    env_val = os.environ.get(name)
    if env_val:
        return env_val
    if _KEYRING_AVAILABLE:
        try:
            kv = keyring.get_password(SERVICE_NAME, name)
            if kv:
                return kv
        except Exception:
            pass
    return default


def set_secret(name: str, value: str) -> bool:
    """Persist a secret via keyring. Returns True on success."""
    if not _KEYRING_AVAILABLE:
        return False
    try:
        keyring.set_password(SERVICE_NAME, name, value)
        return True
    except Exception:
        return False


def keyring_available() -> bool:
    return _KEYRING_AVAILABLE
