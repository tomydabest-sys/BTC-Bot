"""Paper-trading engine + market scan loop.

Each cycle:
  1. Pull active weather events from Gamma (real or mock).
  2. Resolve each event's station via StationResolver.
  3. Fetch forecasts (NWS / Open-Meteo / Met Office, all mock or live).
  4. Run EnsembleBlender per bucket.
  5. Evaluate every strategy → list of Signals.
  6. Risk-gate each Signal, size via quarter-Kelly.
  7. Place a maker limit order (V2 client — mock or live).
  8. Simulate fill + resolution + record TradePair.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
import yaml

from polybot.polyweather.data.climatology.ncei_base_rates import (
    MockNceiBaseRateClient,
    NceiBaseRateClient,
)
from polybot.polyweather.data.forecasts.ensemble_blender import (
    EnsembleBlender,
)
from polybot.polyweather.data.forecasts.met_office_client import (
    MetOfficeClient,
    MockMetOfficeClient,
)
from polybot.polyweather.data.forecasts.nws_client import MockNwsClient, NwsClient
from polybot.polyweather.data.forecasts.open_meteo_client import (
    MockOpenMeteoClient,
    OpenMeteoClient,
)
from polybot.polyweather.data.stations.station_resolver import StationResolver
from polybot.polyweather.exchanges.gamma_client import (
    GammaClient,
    MockGammaClient,
    WeatherEvent,
)
from polybot.polyweather.exchanges.polymarket_v2_client import (
    MockPolymarketV2Client,
    V2Order,
)
from polybot.polyweather.persistence.store import PolyWeatherStore
from polybot.polyweather.risk.validation_gate import TradePair, WeatherValidationGate
from polybot.polyweather.risk.weather_risk import WeatherRiskConfig, WeatherRiskManager
from polybot.polyweather.strategies.negative_risk_arb import (
    EventBucketsView,
    NegativeRiskArbStrategy,
)
from polybot.polyweather.strategies.resolution_meanrev import ResolutionMeanRevStrategy
from polybot.polyweather.strategies.weather_ensemble import (
    WeatherEnsembleStrategy,
    WeatherMarketView,
)

logger = structlog.get_logger()


@dataclass
class EngineMetrics:
    cycles: int = 0
    signals_total: int = 0
    fills_total: int = 0
    fills_by_strategy: dict[str, int] = field(default_factory=dict)
    cancellations: int = 0
    heartbeat_count: int = 0
    last_cycle_at: float = 0.0
    started_at: float = field(default_factory=time.time)
    last_error: str = ""


@dataclass
class EngineConfig:
    mode: str = "paper"
    use_mock: bool = True
    cycle_seconds: float = 5.0
    duration_seconds: float | None = None
    risk: WeatherRiskConfig = field(default_factory=WeatherRiskConfig)
    strategy_weights: dict[str, float] = field(default_factory=dict)
    edge_thresholds_bps: dict[str, float] = field(default_factory=dict)
    confidence_minimums: dict[str, float] = field(default_factory=dict)
    target_cities: list[dict[str, Any]] = field(default_factory=list)
    bankroll_cap_usdc: Decimal | None = None
    first_24h_position_cap_usdc: Decimal = Decimal("5")
    # Pacing: at most one new trade per event per cycle, and at most one trade
    # per bucket within ``bucket_cooldown_seconds``. Stops the engine from
    # firing the same signal every 5s and gives the dashboard a realistic
    # cadence even in mock mode.
    max_signals_per_event_per_cycle: int = 1
    bucket_cooldown_seconds: float = 300.0  # 5 min

    @classmethod
    def from_files(
        cls,
        risk_yaml: Path,
        markets_yaml: Path,
        weights_yaml: Path,
        *,
        mode: str = "paper",
        use_mock: bool = True,
        cycle_seconds: float = 5.0,
        duration_seconds: float | None = None,
    ) -> EngineConfig:
        risk = WeatherRiskConfig.from_yaml(yaml.safe_load(Path(risk_yaml).read_text()))
        markets = yaml.safe_load(Path(markets_yaml).read_text())
        weights = yaml.safe_load(Path(weights_yaml).read_text())
        return cls(
            mode=mode,
            use_mock=use_mock,
            cycle_seconds=cycle_seconds,
            duration_seconds=duration_seconds,
            risk=risk,
            strategy_weights=dict(weights["weights"]),
            edge_thresholds_bps=dict(weights["edge_threshold_bps"]),
            confidence_minimums=dict(weights["confidence_minimum"]),
            target_cities=list(markets.get("target_cities", [])),
        )


class PolyWeatherEngine:
    """The paper-trading core loop. Designed to run in mock for the e2e test."""

    def __init__(
        self,
        config: EngineConfig,
        store: PolyWeatherStore,
        station_resolver: StationResolver | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.metrics = EngineMetrics()
        self.risk = WeatherRiskManager(config.risk, mode=config.mode)
        self.station_resolver = station_resolver or StationResolver()

        # Strategies
        self.s_ensemble = WeatherEnsembleStrategy(
            edge_threshold_bps=config.edge_thresholds_bps.get("weather_ensemble", 800),
            confidence_min=config.confidence_minimums.get("weather_ensemble", 0.55),
            kelly_multiplier=float(config.risk.kelly_fraction_multiplier),
        )
        self.s_arb = NegativeRiskArbStrategy(
            min_gap_bps=config.edge_thresholds_bps.get("negative_risk_arb", 200),
            confidence_min=config.confidence_minimums.get("negative_risk_arb", 0.95),
        )
        self.s_meanrev = ResolutionMeanRevStrategy(
            edge_threshold_bps=config.edge_thresholds_bps.get("resolution_meanrev", 500),
            confidence_min=config.confidence_minimums.get("resolution_meanrev", 0.85),
        )

        # Clients
        if config.use_mock:
            self.gamma = MockGammaClient()
            self.nws = MockNwsClient()
            self.open_meteo = MockOpenMeteoClient()
            self.met_office = MockMetOfficeClient()
            self.ncei = MockNceiBaseRateClient()
            self.exchange = MockPolymarketV2Client()
        else:
            self.gamma = GammaClient()
            self.nws = NwsClient()
            self.open_meteo = OpenMeteoClient()
            self.met_office = MetOfficeClient()
            self.ncei = NceiBaseRateClient()
            # Live exchange instantiated on demand by scripts/live_run.py
            self.exchange = None  # type: ignore[assignment]

        self.blender = EnsembleBlender()
        self._rng = random.Random(20260615)
        self._stop = asyncio.Event()
        self._open_paper_positions: list[dict[str, Any]] = []
        # market_id → last fill timestamp; honoured by ``_bucket_in_cooldown``
        self._last_fill_ts: dict[str, float] = {}
        # Inject a deterministic equity tick at startup so the dashboard
        # always shows something.
        self.store.record_equity(
            self.risk.state.current_bankroll, Decimal("0"), Decimal("0")
        )

    @property
    def is_mock(self) -> bool:
        return self.config.use_mock

    # ─── run loop ────────────────────────────────────────────────────

    async def start(self) -> None:
        if hasattr(self.exchange, "start_heartbeat"):
            self.exchange.start_heartbeat()
        deadline: float | None = None
        if self.config.duration_seconds:
            deadline = time.time() + self.config.duration_seconds
        try:
            while not self._stop.is_set():
                await self._run_cycle()
                if deadline is not None and time.time() >= deadline:
                    break
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.config.cycle_seconds)
                except TimeoutError:
                    continue
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._stop.set()
        if self.exchange is not None and hasattr(self.exchange, "stop_heartbeat"):
            with contextlib.suppress(Exception):
                await self.exchange.stop_heartbeat()
            self.metrics.heartbeat_count = getattr(self.exchange, "heartbeat_count", 0)

    async def _run_cycle(self) -> None:
        self.metrics.cycles += 1
        self.metrics.last_cycle_at = time.time()
        cycle_id = f"{self.metrics.cycles:05d}"
        try:
            events = await self.gamma.list_active_weather_markets()
        except Exception as exc:  # noqa: BLE001
            logger.warning("gamma_fetch_failed", error=str(exc))
            self.metrics.last_error = str(exc)
            return

        for event in events:
            station = self.station_resolver.resolve(event.id, event.rules)
            if station is None:
                self.store.record_decision(
                    cycle_id=cycle_id,
                    strategy="orchestrator",
                    market_id=event.id,
                    decision="SKIPPED",
                    reason="unresolvable_station",
                )
                continue
            await self._evaluate_event(cycle_id, event, station)

        self.store.record_equity(
            self.risk.state.current_bankroll,
            self.risk.state.daily_pnl,
            self.risk.state.open_exposure,
        )
        # Heartbeat freshness mirror so the dashboard can read it directly
        self.metrics.heartbeat_count = getattr(self.exchange, "heartbeat_count", 0)

    def _bucket_in_cooldown(self, market_id: str) -> bool:
        last = self._last_fill_ts.get(market_id, 0.0)
        if last == 0.0:
            return False
        return (time.time() - last) < self.config.bucket_cooldown_seconds

    async def _evaluate_event(self, cycle_id: str, event: WeatherEvent, station) -> None:  # noqa: C901
        # Forecasts (mock or live)
        forecast_om = await self.open_meteo.forecast(station.lat, station.lon)
        nws_points = None
        if station.source == "NWS":
            try:
                nws_points = await self.nws.forecast(station.lat, station.lon)
            except Exception as exc:  # noqa: BLE001
                logger.warning("nws_failed", error=str(exc))

        end_dt = _parse_iso(event.end_date)
        horizon_h = max(2.0, (end_dt - datetime.now(UTC)).total_seconds() / 3600.0)

        # Collect every candidate signal across all strategies + buckets,
        # then pick the highest-edge one(s) per event per cycle. This is what
        # gives the bot a realistic cadence instead of firing 30 trades at once.
        candidates: list[tuple[float, Any, float]] = []  # (edge_bps, signal, p_realised)

        # Negative-risk arb on the basket
        arb_view = EventBucketsView(
            event_id=event.id,
            station=station.icao,
            city=station.city,
            buckets=[
                {
                    "id": b.id, "best_ask": b.best_ask, "best_bid": b.best_bid,
                    "bucket_low": b.bucket_low, "bucket_high": b.bucket_high,
                    "token_id_yes": b.token_id_yes, "token_id_no": b.token_id_no,
                }
                for b in event.buckets
            ],
        )
        for sig in self.s_arb.evaluate(arb_view):
            if self._bucket_in_cooldown(sig.market_id):
                continue
            edge = float(sig.metadata.get("edge_bps", 0.0))
            p_real = float(sig.metadata.get("fair_value", 0.2))
            candidates.append((edge, sig, p_real))

        # Per-bucket ensemble + meanrev
        for bucket in event.buckets:
            if self._bucket_in_cooldown(bucket.id):
                continue
            base_rate = self.ncei.bucket_base_rate(
                station.icao, end_dt.date().isoformat(), bucket.bucket_low, bucket.bucket_high
            )
            forecast = self.blender.blend_open_meteo(
                forecast_om,
                nws_points,
                bucket_low=bucket.bucket_low,
                bucket_high=bucket.bucket_high,
                target_horizon_h=horizon_h,
                base_rate=base_rate,
            )
            view = WeatherMarketView(
                market_id=bucket.id,
                event_id=event.id,
                station=station.icao,
                city=station.city,
                token_id_yes=bucket.token_id_yes,
                token_id_no=bucket.token_id_no,
                bucket_low=bucket.bucket_low,
                bucket_high=bucket.bucket_high,
                best_bid=bucket.best_bid,
                best_ask=bucket.best_ask,
                horizon_hours=horizon_h,
                forecast=forecast,
            )

            sig = self.s_ensemble.evaluate(view)
            if sig is not None:
                edge = float(sig.metadata.get("edge_bps", 0.0))
                candidates.append((edge, sig, forecast.p_bucket))

            sig_mr = self.s_meanrev.evaluate(view)
            if sig_mr is not None:
                edge = float(sig_mr.metadata.get("edge_bps", 0.0))
                candidates.append((edge, sig_mr, forecast.p_bucket))

        # Order by best edge first; fire at most ``max_signals_per_event_per_cycle``
        # winners. Skip duplicates on the same bucket (e.g. ensemble + meanrev
        # both wanting the same market). If the top candidate sizes to zero
        # we continue down the list rather than wasting the event slot.
        candidates.sort(key=lambda c: c[0], reverse=True)
        fired_market_ids: set[str] = set()
        fired = 0
        for edge_bps, sig, p_real in candidates:
            if fired >= self.config.max_signals_per_event_per_cycle:
                break
            if sig.market_id in fired_market_ids:
                continue
            # Pre-flight sizing: if it would round to 0 we skip silently rather
            # than emitting a BLOCKED-sized_zero decision row per candidate.
            target_price = Decimal(str(sig.target_price)).quantize(Decimal("0.0001"))
            p_model = float(
                (sig.metadata or {}).get("model_probability", p_real)
            )
            preview_size = self.risk.quarter_kelly_size(p_model, target_price)
            if preview_size <= 0:
                continue
            self.metrics.signals_total += 1
            await self._handle_signal(cycle_id, event, station, sig, p_realised=p_real)
            fired_market_ids.add(sig.market_id)
            fired += 1

    # ─── signal handling ─────────────────────────────────────────────

    async def _handle_signal(
        self,
        cycle_id: str,
        event: WeatherEvent,
        station,
        signal,
        *,
        p_realised: float,
    ) -> None:
        target_price = Decimal(str(signal.target_price)).quantize(Decimal("0.0001"))
        meta = signal.metadata or {}
        p_model = float(meta.get("model_probability", meta.get("fair_value", p_realised)))

        # Apply quarter-Kelly sizing via the risk module
        size_usdc = self.risk.quarter_kelly_size(p_model, target_price)
        if size_usdc <= 0:
            self.store.record_decision(
                cycle_id=cycle_id,
                strategy=signal.strategy,
                market_id=signal.market_id,
                station=station.icao,
                city=station.city,
                decision="BLOCKED",
                reason="sized_zero",
                mid=float(target_price),
                confidence=signal.confidence,
                edge_bps=float(meta.get("edge_bps", 0.0)),
                model_probability=p_model,
                bucket_low=float(meta.get("bucket_low", 0)),
                bucket_high=float(meta.get("bucket_high", 0)),
                forecast_horizon_hours=float(meta.get("horizon_hours", 0)),
                extra={"size_usdc": "0"},
            )
            return

        ok, reason = self.risk.can_open(size_usdc)
        if not ok:
            self.store.record_decision(
                cycle_id=cycle_id,
                strategy=signal.strategy,
                market_id=signal.market_id,
                station=station.icao,
                city=station.city,
                decision="BLOCKED",
                reason=reason,
                mid=float(target_price),
                confidence=signal.confidence,
                edge_bps=float(meta.get("edge_bps", 0.0)),
                model_probability=p_model,
                bucket_low=float(meta.get("bucket_low", 0)),
                bucket_high=float(meta.get("bucket_high", 0)),
                forecast_horizon_hours=float(meta.get("horizon_hours", 0)),
            )
            return

        # Place order on the exchange (mock or live)
        size_tokens = (size_usdc / target_price).quantize(Decimal("0.0001"))
        order = V2Order(
            token_id=meta.get("token_id", ""),
            side=signal.direction.value,
            price=target_price,
            size=size_tokens,
            metadata={"strategy": signal.strategy, "market_id": signal.market_id},
        )
        receipt = await self.exchange.place_order(order)
        if not receipt.accepted:
            self.store.record_decision(
                cycle_id=cycle_id,
                strategy=signal.strategy,
                market_id=signal.market_id,
                station=station.icao,
                city=station.city,
                decision="BLOCKED",
                reason=f"order_rejected:{receipt.reason}",
            )
            return

        # Simulate fill + resolution in paper mode
        self.risk.record_open(size_usdc)
        outcome = 1 if self._rng.random() < p_realised else 0
        if outcome == 1:
            exit_price = Decimal("1.00")
        else:
            exit_price = Decimal("0.00")
        realised_pnl = (exit_price - target_price) * size_tokens
        # Maker rebate: tiny positive on every fill (modeled at 5 bps notional)
        rebate = (size_usdc * Decimal("0.0005")).quantize(Decimal("0.0001"))
        realised_pnl = (realised_pnl + rebate).quantize(Decimal("0.0001"))

        trade = TradePair(
            market_id=signal.market_id,
            event_id=event.id,
            strategy=signal.strategy,
            station=station.icao,
            city=station.city,
            side=signal.direction.value,
            entry_price=target_price,
            exit_price=exit_price,
            size=size_tokens,
            fees_usdc=Decimal("0"),
            rebates_usdc=rebate,
            realised_pnl_usdc=realised_pnl,
            opened_at=time.time(),
            closed_at=time.time() + 1.0,
            fill_latency_seconds=0.5 + self._rng.random() * 2.0,
            model_probability=p_model,
            realised_outcome=outcome,
            used_dynamic_fee=True,
            cap_violation=size_usdc > self.risk.weather_position_cap_usdc(),
            metadata={
                "edge_bps": float(meta.get("edge_bps", 0.0)),
                "horizon_hours": float(meta.get("horizon_hours", 0.0)),
                "model_contributions": meta.get("model_contributions", {}),
            },
        )
        self.store.record_trade(trade)
        self.risk.record_close(size_usdc, realised_pnl)
        self.metrics.fills_total += 1
        self.metrics.fills_by_strategy[signal.strategy] = (
            self.metrics.fills_by_strategy.get(signal.strategy, 0) + 1
        )
        # Cool the bucket so the same market isn't retraded next cycle.
        self._last_fill_ts[signal.market_id] = time.time()

        self.store.record_decision(
            cycle_id=cycle_id,
            strategy=signal.strategy,
            market_id=signal.market_id,
            station=station.icao,
            city=station.city,
            decision="EXECUTED",
            reason=signal.reason,
            mid=float(target_price),
            confidence=signal.confidence,
            edge_bps=float(meta.get("edge_bps", 0.0)),
            model_probability=p_model,
            bucket_low=float(meta.get("bucket_low", 0)),
            bucket_high=float(meta.get("bucket_high", 0)),
            forecast_horizon_hours=float(meta.get("horizon_hours", 0)),
            extra={"size_usdc": str(size_usdc), "pnl_usdc": str(realised_pnl)},
        )


def _parse_iso(s: str) -> datetime:
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def build_validation_gate(store: PolyWeatherStore, station_resolver: StationResolver) -> dict:
    gate = WeatherValidationGate()
    trades = store.trades(limit=10000)
    first_ts = store.first_trade_ts() or time.time()
    paper_days = max(0.0, (time.time() - first_ts) / 86400.0)
    equity = [b for _, b in store.equity_history(limit=10000)]
    if not equity:
        equity = [Decimal("1260")]
    audit_stats = station_resolver.accuracy_stats()
    return gate.check(trades, paper_days=paper_days, equity_curve=equity, station_audit=audit_stats)
