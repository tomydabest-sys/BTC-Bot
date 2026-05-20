"""FastAPI dashboard for the Polymarket trading bot.

Fixes:
- HTML served with explicit UTF-8 encoding and charset declaration (prevents
  the "â€"" / "Â·" mojibake on Windows when read_text() defaults to cp1252).
- Robust handling of _bot being None in WebSocket push loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from polybot.dashboard.analytics import get_full_analytics

logger = structlog.get_logger()

_bot = None
_trade_log: list[dict] = []
_signal_log: list[dict] = []

MAX_LOG_SIZE = 500

_terminal_clients: list[WebSocket] = []
_terminal_buffer: deque[str] = deque(maxlen=500)


class WebSocketLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            _terminal_buffer.append(msg)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_broadcast_terminal(msg))
            except RuntimeError:
                pass
        except Exception:
            pass


async def _broadcast_terminal(line: str) -> None:
    if not _terminal_clients:
        return
    disconnected = []
    payload = json.dumps({"type": "log", "data": line})
    for ws in _terminal_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in _terminal_clients:
            _terminal_clients.remove(ws)


def set_bot(bot) -> None:
    global _bot
    _bot = bot


def get_bot():
    return _bot


app = FastAPI(title="PolyBot Dashboard", version="1.0.0")


def create_app(bot=None, config=None):
    """Factory used by the launcher.

    Wires the running Bot (and optional Config) into the module-level FastAPI
    instance so the existing route handlers can reach them via get_bot().
    """
    if bot is not None:
        set_bot(bot)
    if config is not None:
        # Stash for routes that may want to read config without importing main
        app.state.config = config
    return app

_ws_clients: list[WebSocket] = []


async def broadcast(data: dict) -> None:
    message = json.dumps(data, default=str)
    disconnected = []
    for ws in _ws_clients:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


@app.get("/api/status")
async def get_status() -> dict:
    bot = get_bot()
    if not bot:
        return {"running": False, "mode": "unknown"}
    return {
        "running": bot.running,
        "mode": bot.config.bot.mode,
        "name": bot.config.bot.name,
        "trading_allowed": bot.circuit_breaker.is_trading_allowed,
        "size_multiplier": bot.circuit_breaker.size_multiplier,
        "active_markets": len(bot.scanner.active_markets),
        "strategies_loaded": [s.name for s in bot.strategies],
    }


@app.get("/api/portfolio")
async def get_portfolio() -> dict:
    bot = get_bot()
    if not bot:
        return {"positions": [], "realized_pnl": 0, "daily_pnl": 0}
    portfolio = bot.position_manager.get_portfolio()
    return {
        "positions": [
            {
                "market_id": p.market_id,
                "token_id": p.token_id,
                "outcome": p.outcome,
                "side": p.side.value,
                "size": round(p.size, 4),
                "avg_entry_price": round(p.avg_entry_price, 4),
                "current_price": round(p.current_price, 4),
                "unrealized_pnl": round(p.unrealized_pnl, 4),
                "notional": round(p.notional, 2),
                "strategy": p.strategy,
                "opened_at": p.opened_at.isoformat(),
            }
            for p in portfolio.positions
        ],
        "realized_pnl": round(portfolio.realized_pnl, 2),
        "daily_pnl": round(portfolio.daily_pnl, 2),
        "unrealized_pnl": round(portfolio.unrealized_pnl, 2),
        "total_pnl": round(portfolio.total_pnl, 2),
        "total_exposure": round(portfolio.total_exposure, 2),
        "balance": round(portfolio.balance, 2),
        "num_positions": len(portfolio.positions),
    }


@app.get("/api/markets")
async def get_markets() -> dict:
    bot = get_bot()
    if not bot:
        return {"markets": []}
    markets = bot.scanner.active_markets
    result = []
    for market_id, market in markets.items():
        snapshot = bot.data_pipeline.get_snapshot(market_id)
        entry: dict[str, Any] = {
            "id": market.id,
            "question": market.question,
            "category": market.category,
            "volume_24h": round(market.volume_24h, 2),
            "liquidity": round(market.liquidity, 2),
            "end_date": market.end_date.isoformat(),
        }
        if snapshot:
            entry["mid_price"] = round(snapshot.orderbook.mid_price, 4)
            entry["spread"] = round(snapshot.orderbook.spread, 4)
            entry["book_imbalance"] = round(snapshot.orderbook.book_imbalance, 4)
            entry["vwap_1h"] = round(snapshot.vwap_1h, 4)
            entry["volatility_1h"] = round(snapshot.volatility_1h, 6)
        result.append(entry)
    return {"markets": result, "count": len(result)}


@app.get("/api/strategies")
async def get_strategies() -> dict:
    bot = get_bot()
    if not bot:
        return {"strategies": []}
    return {
        "strategies": [
            {"name": s.name, "params": s.get_params()}
            for s in bot.strategies
        ],
        "available": list(
            {"mean_reversion", "momentum", "latency_arb", "momentum_lag",
             "volatility_breakout", "dual_direction_arb", "market_maker",
             "monte_carlo", "calibration_edge", "maker_edge",
             "overshoot_reversion"}
        ),
    }


@app.get("/api/risk")
async def get_risk() -> dict:
    bot = get_bot()
    if not bot:
        return {}
    cfg = bot.config.risk
    cb = bot.circuit_breaker
    portfolio = bot.position_manager.get_portfolio()
    return {
        "config": {
            "max_position_size": cfg.max_position_size,
            "max_portfolio_exposure": cfg.max_portfolio_exposure,
            "max_positions": cfg.max_positions,
            "max_daily_loss": cfg.max_daily_loss,
            "max_order_size": cfg.max_order_size,
            "max_slippage_pct": cfg.max_slippage_pct,
            "min_trade_interval_seconds": cfg.min_trade_interval_seconds,
        },
        "circuit_breaker": {
            "trading_allowed": cb.is_trading_allowed,
            "size_multiplier": round(cb.size_multiplier, 4),
        },
        "current": {
            "total_exposure": round(portfolio.total_exposure, 2),
            "daily_pnl": round(portfolio.daily_pnl, 2),
            "num_positions": len(portfolio.positions),
            "exposure_pct": round(
                portfolio.total_exposure / cfg.max_portfolio_exposure * 100, 1
            ) if cfg.max_portfolio_exposure > 0 else 0,
            "daily_loss_pct": round(
                abs(min(portfolio.daily_pnl, 0)) / cfg.max_daily_loss * 100, 1
            ) if cfg.max_daily_loss > 0 else 0,
        },
    }


@app.get("/api/exchange-prices")
async def get_exchange_prices() -> dict:
    bot = get_bot()
    if not bot:
        return {"prices": {}}
    prices = {}
    for symbol, feed in bot.exchange_feed.feeds.items():
        feed_age = getattr(feed, "feed_age_s", None)
        prices[symbol] = {
            "price": round(feed.last_price, 2),
            "last_update": feed.last_update,
            "feed_age_s": round(feed_age, 1) if feed_age is not None else None,
            "is_stale": bool(getattr(feed, "is_stale", False)),
            "last_source": getattr(feed, "last_source", ""),
            "change_5s": round(feed.price_change_pct(5) * 100, 3),
            "change_30s": round(feed.price_change_pct(30) * 100, 3),
            "change_60s": round(feed.price_change_pct(60) * 100, 3),
            "volatility_60s": round(feed.volatility_window(60), 4),
            "momentum": round(feed.momentum_score() * 100, 3),
        }
    return {"prices": prices}


@app.get("/api/trades")
async def get_trades() -> dict:
    return {"trades": _trade_log[-100:], "total": len(_trade_log)}


@app.get("/api/signals")
async def get_signals() -> dict:
    return {"signals": _signal_log[-100:], "total": len(_signal_log)}


@app.get("/api/analytics")
async def api_analytics():
    db_path = "./data/bot.db"
    try:
        return get_full_analytics(db_path)
    except Exception as e:
        return {"error": str(e), "edge": {}, "signal_analysis": {}, "risk": {}}


@app.get("/api/validation")
async def api_validation() -> dict:
    """Paper-validation gate state + per-metric verdict.

    Returns enabled=false when maker mode (and therefore the gate) is off.
    """
    bot = get_bot()
    if bot is None or bot.validation_gate is None:
        return {"enabled": False}
    report = bot.validation_gate.evaluate()
    return {"enabled": True, **report.as_dict()}


@app.get("/api/maker")
async def api_maker() -> dict:
    """Maker-mode vitals: quote uptime, fills, inventory, latency.

    Returns enabled=false when the bot is running in legacy taker mode
    so the dashboard can suppress the Maker tab.
    """
    bot = get_bot()
    if bot is None or bot.maker is None:
        return {"enabled": False}
    v = bot.maker.vitals()
    market_states = []
    for state in bot.maker.markets.values():
        market_states.append({
            "market_id": state.market.id,
            "slug": state.market.slug,
            "question": state.market.question[:80],
            "strike": round(state.strike, 2),
            "last_mid": round(state.last_mid, 4),
            "net_inventory_shares": state.inventory.net_yes_shares,
            "fills_today": state.fills_today,
            "resting_orders": len(state.quote_manager.state.resting),
        })
    return {
        "enabled": True,
        "vitals": {
            "quote_uptime_pct": round(v.quote_uptime_pct, 1),
            "fills_today": v.fills_today,
            "net_inventory_shares": v.net_inventory_shares,
            "p95_latency_ms": round(v.p95_latency_ms, 1),
            "active_markets": v.active_markets,
            "latency_breaching": v.extras.get("latency_breaching", False),
        },
        "markets": market_states,
    }


@app.post("/api/bot/stop")
async def stop_bot() -> dict:
    bot = get_bot()
    if bot and bot.running:
        asyncio.create_task(bot.stop())
        return {"status": "stopping"}
    return {"status": "not_running"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    _ws_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in _ws_clients:
            _ws_clients.remove(websocket)


@app.websocket("/ws/terminal")
async def terminal_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    _terminal_clients.append(websocket)
    for line in _terminal_buffer:
        try:
            await websocket.send_text(json.dumps({"type": "log", "data": line}))
        except Exception:
            break
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in _terminal_clients:
            _terminal_clients.remove(websocket)


async def _push_updates_loop() -> None:
    while True:
        try:
            bot = get_bot()
            if bot and bot.running and _ws_clients:
                portfolio = bot.position_manager.get_portfolio()
                prices = {}
                for symbol, feed in bot.exchange_feed.feeds.items():
                    prices[symbol] = {
                        "price": round(feed.last_price, 2),
                        "change_60s": round(feed.price_change_pct(60) * 100, 3),
                        "momentum": round(feed.momentum_score() * 100, 3),
                    }
                await broadcast({
                    "type": "update",
                    "timestamp": datetime.utcnow().isoformat(),
                    "portfolio": {
                        "realized_pnl": round(portfolio.realized_pnl, 2),
                        "daily_pnl": round(portfolio.daily_pnl, 2),
                        "unrealized_pnl": round(portfolio.unrealized_pnl, 2),
                        "total_pnl": round(portfolio.total_pnl, 2),
                        "total_exposure": round(portfolio.total_exposure, 2),
                        "num_positions": len(portfolio.positions),
                    },
                    "exchange_prices": prices,
                    "trading_allowed": bot.circuit_breaker.is_trading_allowed,
                    "active_markets": len(bot.scanner.active_markets),
                })
        except Exception as e:
            logger.debug("push_update_error", error=str(e))
        await asyncio.sleep(2)


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.create_task(_push_updates_loop())


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard() -> HTMLResponse:
    """Serve the dashboard HTML with explicit UTF-8 encoding.

    Windows Path.read_text() defaults to cp1252, which corrupts em-dashes
    and middle-dots ("—" → "â€"", "·" → "Â·"). Reading and declaring UTF-8
    fixes the mojibake.
    """
    html_path = Path(__file__).parent / "frontend" / "index.html"
    if html_path.exists():
        content = html_path.read_text(encoding="utf-8")
        return HTMLResponse(
            content=content,
            media_type="text/html; charset=utf-8",
            # The JS is inlined in this document. Without no-store the browser
            # can serve a stale copy after a code update (e.g. switching
            # branches), and the old JS throws on the new API payloads —
            # which silently bricks the refresh loop and hides tabs.
            headers={"Cache-Control": "no-store, must-revalidate"},
        )
    return HTMLResponse(
        content="<h1>Dashboard frontend not found</h1>",
        media_type="text/html; charset=utf-8",
    )


def log_trade(trade_data: dict) -> None:
    _trade_log.append({**trade_data, "timestamp": datetime.utcnow().isoformat()})
    if len(_trade_log) > MAX_LOG_SIZE:
        _trade_log.pop(0)


def log_signal(signal_data: dict) -> None:
    _signal_log.append({**signal_data, "timestamp": datetime.utcnow().isoformat()})
    if len(_signal_log) > MAX_LOG_SIZE:
        _signal_log.pop(0)
