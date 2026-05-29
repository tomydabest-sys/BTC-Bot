"""Weather-mode FastAPI routes + standalone app factory.

Seven endpoints under ``/api/weather/*`` and a static HTML/JS/CSS frontend
served from ``frontend/``. Every route must respond in <500ms with non-empty
JSON in mock mode.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from polybot.polyweather.data.stations.station_resolver import StationResolver
from polybot.polyweather.orchestrator.engine import (
    PolyWeatherEngine,
    build_validation_gate,
)
from polybot.polyweather.persistence.store import PolyWeatherStore, decimal_default

FRONTEND_DIR = Path(__file__).parent / "frontend"


class _Holder:
    engine: PolyWeatherEngine | None = None
    store: PolyWeatherStore | None = None
    station_resolver: StationResolver | None = None
    mode: str = "paper"
    mock: bool = True


HOLDER = _Holder()


class DecimalJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(content, default=decimal_default).encode("utf-8")


def _bot_running() -> bool:
    return HOLDER.engine is not None


def attach(
    app: FastAPI,
    *,
    engine: PolyWeatherEngine,
    store: PolyWeatherStore,
    station_resolver: StationResolver,
    mode: str = "paper",
    mock: bool = True,
) -> None:
    HOLDER.engine = engine
    HOLDER.store = store
    HOLDER.station_resolver = station_resolver
    HOLDER.mode = mode
    HOLDER.mock = mock
    _register_routes(app)


def create_app(
    engine: PolyWeatherEngine,
    store: PolyWeatherStore,
    station_resolver: StationResolver,
    mode: str = "paper",
    mock: bool = True,
) -> FastAPI:
    app = FastAPI(title="PolyWeather Dashboard", version="1.0.0")
    attach(app, engine=engine, store=store, station_resolver=station_resolver, mode=mode, mock=mock)
    return app


def _register_routes(app: FastAPI) -> None:  # noqa: C901 — registers 9 endpoints
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        path = FRONTEND_DIR / "index.html"
        if not path.exists():
            return HTMLResponse(
                "<h1>PolyWeather-Bot</h1><p>Dashboard frontend missing.</p>",
                status_code=200,
            )
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/app.js")
    async def appjs() -> PlainTextResponse:
        path = FRONTEND_DIR / "app.js"
        if not path.exists():
            return PlainTextResponse("// app.js missing", media_type="application/javascript")
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="application/javascript")

    @app.get("/styles.css")
    async def stylescss() -> PlainTextResponse:
        path = FRONTEND_DIR / "styles.css"
        if not path.exists():
            return PlainTextResponse("/* styles missing */", media_type="text/css")
        return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/css")

    @app.get("/api/weather/overview", response_class=DecimalJSONResponse)
    async def weather_overview() -> Any:
        engine = HOLDER.engine
        store = HOLDER.store
        if engine is None or store is None:
            return _empty_overview()
        try:
            eq = store.equity_history(limit=2000)
        except Exception:  # noqa: BLE001
            eq = []
        bankroll = engine.risk.state.current_bankroll
        open_exp = engine.risk.state.open_exposure
        unrealized = engine.unrealized_pnl_usdc
        open_positions = engine.open_positions
        # P&L windows
        now_ts = time.time()
        pnl_24h = _pnl_since(eq, now_ts - 86400)
        pnl_7d = _pnl_since(eq, now_ts - 7 * 86400)
        pnl_30d = _pnl_since(eq, now_ts - 30 * 86400)
        # Open positions: paper engine resolves immediately so 0 in v1
        try:
            trades = store.trades(limit=2000)
        except Exception:  # noqa: BLE001
            trades = []
        brier = _brier_window(trades, days=30)
        sharpe = _sharpe_window(eq)
        hb_ts = getattr(engine.exchange, "last_heartbeat_ts", 0.0)
        hb_age = (now_ts - hb_ts) if hb_ts else 9999.0
        hb_status = "green" if hb_age <= 5 else ("amber" if hb_age <= 10 else "red")
        if hb_ts == 0:
            hb_status = "amber" if engine.metrics.cycles == 0 else "red"
        halted, halt_reason = engine.risk.check_kill_switch()
        return {
            "bankroll_usdc": bankroll,
            "pnl_24h_usdc": pnl_24h,
            "pnl_24h_pct": float(pnl_24h / bankroll * 100) if bankroll else 0.0,
            "pnl_7d_usdc": pnl_7d,
            "pnl_30d_usdc": pnl_30d,
            "open_positions": len(open_positions),
            "unrealized_pnl_usdc": unrealized,
            "open_exposure_usdc": open_exp,
            "open_exposure_pct": float(open_exp / bankroll * 100) if bankroll else 0.0,
            "brier_30d": brier,
            "sharpe_30d": sharpe,
            "equity_curve": [{"ts": ts, "bankroll": b} for ts, b in eq],
            "drawdown_curve": _drawdown_series(eq),
            "signal_count_today": engine.metrics.signals_total,
            "fill_count_today": engine.metrics.fills_total,
            "convergence_exits": engine.metrics.convergence_exits,
            "heartbeat": {
                "last_ts": hb_ts,
                "age_seconds": hb_age,
                "status": hb_status,
                "count": getattr(engine.exchange, "heartbeat_count", 0),
            },
            "mode": _mode_label(engine, HOLDER.mode, HOLDER.mock, halted),
            "halt_reason": halt_reason,
        }

    @app.get("/api/weather/per-city-edge", response_class=DecimalJSONResponse)
    async def per_city_edge() -> Any:
        engine = HOLDER.engine
        store = HOLDER.store
        if engine is None or store is None:
            return {"cells": []}
        rows = store.decisions(limit=2000)
        # Group by city × bucket
        grid: dict[tuple[str, float, float], dict[str, Any]] = {}
        for r in rows:
            key = (r.get("city") or "?", float(r.get("bucket_low") or 0), float(r.get("bucket_high") or 0))
            cell = grid.setdefault(
                key,
                {
                    "city": key[0],
                    "bucket_low": key[1],
                    "bucket_high": key[2],
                    "edge_bps": r.get("edge_bps") or 0.0,
                    "fill_probability": (r.get("model_probability") or 0.0),
                    "market_id": r.get("market_id"),
                    "samples": 0,
                    "executed": 0,
                },
            )
            cell["samples"] += 1
            if r.get("decision") == "EXECUTED":
                cell["executed"] += 1
            if r.get("edge_bps") is not None:
                cell["edge_bps"] = max(cell["edge_bps"], r.get("edge_bps"))
        return {"cells": list(grid.values())}

    @app.get("/api/weather/forecasts", response_class=DecimalJSONResponse)
    async def forecasts() -> Any:
        engine = HOLDER.engine
        store = HOLDER.store
        if engine is None or store is None:
            return {"markets": []}
        decisions = store.decisions(limit=500)
        # Aggregate the most-recent decision per market
        latest: dict[str, dict[str, Any]] = {}
        for d in decisions:
            mid = d.get("market_id")
            if mid is None or mid in latest:
                continue
            latest[mid] = d
        markets = []
        for d in latest.values():
            markets.append(
                {
                    "city": d.get("city"),
                    "market_id": d.get("market_id"),
                    "strategy": d.get("strategy"),
                    "decision": d.get("decision"),
                    "model_probability": d.get("model_probability"),
                    "edge_bps": d.get("edge_bps"),
                    "confidence": d.get("confidence"),
                    "horizon_hours": d.get("forecast_horizon_hours"),
                    "bucket_low": d.get("bucket_low"),
                    "bucket_high": d.get("bucket_high"),
                    "ts": d.get("ts"),
                }
            )
        return {"markets": markets}

    @app.get("/api/weather/risk", response_class=DecimalJSONResponse)
    async def risk() -> Any:
        engine = HOLDER.engine
        store = HOLDER.store
        if engine is None or store is None:
            return _empty_risk()
        trades = store.trades(limit=10000)
        eq = store.equity_history(limit=10000)
        by_strategy: dict[str, Decimal] = {}
        for t in trades:
            by_strategy[t.strategy] = by_strategy.get(t.strategy, Decimal("0")) + t.realised_pnl_usdc
        halted, halt_reason = engine.risk.check_kill_switch()
        max_dd = _max_drawdown([b for _, b in eq])
        peak_ts = max((ts for ts, b in eq if b == engine.risk.state.ath_bankroll), default=None)
        time_since_ath_s = (time.time() - peak_ts) if peak_ts else 0.0
        return {
            "halted": halted,
            "halt_reason": halt_reason,
            "halt_kind": engine.risk.state.halt_kind,
            # Seconds until an auto-recovering halt (daily / consecutive) lifts;
            # None when not halted or when the halt is the permanent ATH kill.
            "halt_recovery_in_seconds": engine.risk.halt_recovery_in_seconds(),
            "current_bankroll_usdc": engine.risk.state.current_bankroll,
            "ath_bankroll_usdc": engine.risk.state.ath_bankroll,
            "max_drawdown_pct": float(max_dd),
            "time_since_ath_seconds": time_since_ath_s,
            "daily_pnl_usdc": engine.risk.state.daily_pnl,
            "weekly_pnl_usdc": engine.risk.state.weekly_pnl,
            "open_exposure_usdc": engine.risk.state.open_exposure,
            "open_exposure_cap_usdc": engine.risk.config.max_total_open_exposure_usdc,
            "consecutive_losses": engine.risk.state.consecutive_losses,
            "kill_switches": [
                {
                    "name": "daily_loss",
                    "current": engine.risk.state.daily_pnl,
                    "threshold": -engine.risk.config.max_daily_loss_usdc,
                    "armed": engine.risk.state.daily_pnl <= -engine.risk.config.max_daily_loss_usdc,
                },
                {
                    "name": "ath_drawdown",
                    "current": float(max_dd),
                    "threshold": float(engine.risk.config.all_time_high_kill_drawdown_pct),
                    "armed": max_dd >= engine.risk.config.all_time_high_kill_drawdown_pct,
                },
                {
                    "name": "consecutive_losses",
                    "current": engine.risk.state.consecutive_losses,
                    "threshold": engine.risk.config.consecutive_loss_pause_count,
                    "armed": engine.risk.state.consecutive_losses
                    >= engine.risk.config.consecutive_loss_pause_count,
                },
            ],
            "pnl_by_strategy": {k: v for k, v in by_strategy.items()},
            "open_positions": [
                {
                    "market_id": p.market_id,
                    "city": p.city,
                    "strategy": p.strategy,
                    "side": p.side,
                    "outcome": p.outcome,
                    "entry_price": p.entry_price,
                    "current_price": p.current_price,
                    "size_usdc": p.size_usdc,
                    "size_tokens": p.size_tokens,
                    "unrealized_pnl_usdc": p.unrealized_pnl,
                    "opened_at": p.opened_at,
                    "closes_at": p.closes_at,
                }
                for p in engine.open_positions
            ],
        }

    @app.get("/api/weather/trades", response_class=DecimalJSONResponse)
    async def trades(limit: int = 50, strategy: str | None = None, city: str | None = None) -> Any:
        store = HOLDER.store
        if store is None:
            return {"trades": []}
        rows = store.trades(limit=limit, strategy=strategy, city=city)
        return {
            "trades": [
                {
                    "timestamp": t.closed_at,
                    "market_id": t.market_id,
                    "strategy": t.strategy,
                    "city": t.city,
                    "side": t.side,
                    "size": t.size,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "pnl_usdc": t.realised_pnl_usdc,
                    "model_probability": t.model_probability,
                    "fill_latency_seconds": t.fill_latency_seconds,
                }
                for t in rows
            ]
        }

    @app.get("/api/weather/validation-gate", response_class=DecimalJSONResponse)
    async def validation_gate() -> Any:
        store = HOLDER.store
        resolver = HOLDER.station_resolver
        if store is None or resolver is None:
            return {"pass": False, "criteria": {}, "ready_for_live": False, "failing": []}
        gate = build_validation_gate(store, resolver)
        failing = [name for name, c in gate["criteria"].items() if not c["pass"]]
        return {
            "pass": gate["pass"],
            "ready_for_live": gate["pass"],
            "criteria": gate["criteria"],
            "failing": failing,
        }

    @app.get("/api/weather/market/{market_id}", response_class=DecimalJSONResponse)
    async def market_detail(market_id: str) -> Any:
        store = HOLDER.store
        if store is None:
            raise HTTPException(status_code=503, detail="bot not running")
        rows = store.decisions(limit=2000)
        for_market = [r for r in rows if r.get("market_id") == market_id]
        trades = store.trades(limit=200)
        for_market_trades = [t for t in trades if t.market_id == market_id]
        return {
            "market_id": market_id,
            "decisions": for_market[:50],
            "trades": [
                {
                    "ts": t.closed_at, "side": t.side,
                    "entry_price": t.entry_price, "exit_price": t.exit_price,
                    "pnl_usdc": t.realised_pnl_usdc,
                    "model_probability": t.model_probability,
                }
                for t in for_market_trades
            ],
            "model_contributions": _latest_model_contributions(for_market),
        }


# ─── helpers ────────────────────────────────────────────────────────


def _empty_overview() -> dict[str, Any]:
    return {
        "bankroll_usdc": Decimal("1260"),
        "pnl_24h_usdc": Decimal("0"),
        "pnl_24h_pct": 0.0,
        "pnl_7d_usdc": Decimal("0"),
        "pnl_30d_usdc": Decimal("0"),
        "open_positions": 0,
        "open_exposure_usdc": Decimal("0"),
        "open_exposure_pct": 0.0,
        "brier_30d": 1.0,
        "sharpe_30d": 0.0,
        "equity_curve": [],
        "drawdown_curve": [],
        "signal_count_today": 0,
        "fill_count_today": 0,
        "convergence_exits": 0,
        "heartbeat": {"last_ts": 0.0, "age_seconds": 9999.0, "status": "red", "count": 0},
        "mode": "MOCK",
        "halt_reason": "",
    }


def _empty_risk() -> dict[str, Any]:
    return {
        "halted": False,
        "halt_reason": "",
        "halt_kind": "",
        "halt_recovery_in_seconds": None,
        "current_bankroll_usdc": Decimal("1260"),
        "ath_bankroll_usdc": Decimal("1260"),
        "max_drawdown_pct": 0.0,
        "time_since_ath_seconds": 0.0,
        "daily_pnl_usdc": Decimal("0"),
        "weekly_pnl_usdc": Decimal("0"),
        "open_exposure_usdc": Decimal("0"),
        "open_exposure_cap_usdc": Decimal("504"),
        "consecutive_losses": 0,
        "kill_switches": [],
        "pnl_by_strategy": {},
        "open_positions": [],
    }


def _pnl_since(eq: list[tuple[float, Decimal]], cutoff_ts: float) -> Decimal:
    if not eq:
        return Decimal("0")
    last = eq[-1][1]
    prev = next((b for ts, b in eq if ts >= cutoff_ts), eq[0][1])
    return (last - prev).quantize(Decimal("0.0001"))


MIN_SAMPLES_FOR_STATS = 30


def _brier_window(trades, days: int) -> float | None:
    cutoff = time.time() - days * 86400
    sample = [t for t in trades if t.closed_at >= cutoff]
    if len(sample) < MIN_SAMPLES_FOR_STATS:
        return None
    return sum((t.model_probability - t.realised_outcome) ** 2 for t in sample) / len(sample)


def _sharpe_window(eq: list[tuple[float, Decimal]]) -> float | None:
    if len(eq) < MIN_SAMPLES_FOR_STATS:
        return None
    rets: list[float] = []
    for (_, a), (_, b) in zip(eq, eq[1:], strict=False):
        if a == 0:
            continue
        rets.append(float((b - a) / a))
    if len(rets) < MIN_SAMPLES_FOR_STATS:
        return None
    import math
    import statistics
    mean = statistics.fmean(rets)
    sd = statistics.pstdev(rets)
    if sd == 0:
        return None
    sharpe = (mean / sd) * math.sqrt(365)
    # Cap reported Sharpe to a believable display range. Anything over ~5
    # in real markets is suspect; over 10 is a UI lie.
    if sharpe > 10:
        sharpe = 10.0
    return sharpe


def _drawdown_series(eq: list[tuple[float, Decimal]]) -> list[dict[str, Any]]:
    if not eq:
        return []
    out = []
    peak = eq[0][1]
    for ts, b in eq:
        if b > peak:
            peak = b
        dd = float((peak - b) / peak) if peak > 0 else 0.0
        out.append({"ts": ts, "drawdown_pct": dd})
    return out


def _max_drawdown(equity: list[Decimal]) -> Decimal:
    if not equity:
        return Decimal("0")
    peak = equity[0]
    worst = Decimal("0")
    for x in equity:
        if x > peak:
            peak = x
        if peak > 0:
            dd = (peak - x) / peak
            if dd > worst:
                worst = dd
    return worst


def _mode_label(engine, mode: str, mock: bool, halted: bool) -> str:
    if halted:
        return "HALTED"
    if mock:
        return "MOCK"
    if getattr(engine.config, "live_data", False):
        return "LIVE-DATA"
    return mode.upper()


def _latest_model_contributions(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    for d in decisions:
        extra = d.get("extra")
        if not extra:
            continue
        try:
            payload = json.loads(extra)
        except Exception:  # noqa: BLE001
            continue
        if "model_contributions" in payload:
            return payload["model_contributions"]
    return {}
