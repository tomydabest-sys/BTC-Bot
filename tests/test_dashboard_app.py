"""Tests for the dashboard FastAPI app surface.

Focused on the regressions that bricked the live dashboard:
  * stale cached JS after a code update (no-store header), and
  * the maker/validation endpoints reporting enabled=false when they
    should be on (which keeps those tabs hidden).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from polybot.dashboard.app import create_app, set_bot


def test_dashboard_html_sends_no_store_header():
    """The inlined JS must never be served stale — switching branches and
    getting the old JS against new payloads is what froze the refresh loop."""
    app = create_app(bot=None, config=None)
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    cache = r.headers.get("cache-control", "")
    assert "no-store" in cache, f"expected no-store, got {cache!r}"


def test_maker_and_validation_endpoints_disabled_without_bot():
    set_bot(None)
    app = create_app(bot=None, config=None)
    client = TestClient(app)
    assert client.get("/api/maker").json() == {"enabled": False}
    assert client.get("/api/validation").json() == {"enabled": False}


def test_maker_endpoint_enabled_with_maker_bot(tmp_path):
    """When a maker-mode bot is wired, /api/maker must report enabled=true
    so the frontend un-hides the Maker tab."""
    from polybot.config import BotConfig, Config, MakerConfig
    from polybot.main import Bot

    cfg = Config(
        bot=BotConfig(mode="paper", data_dir=str(tmp_path / "data")),
        maker=MakerConfig(enabled=True),
    )
    bot = Bot(cfg)
    try:
        app = create_app(bot=bot, config=cfg)
        client = TestClient(app)
        maker = client.get("/api/maker").json()
        assert maker["enabled"] is True
        assert "vitals" in maker
        validation = client.get("/api/validation").json()
        assert validation["enabled"] is True
        assert "metrics" in validation
    finally:
        set_bot(None)
