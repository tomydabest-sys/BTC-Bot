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
from polybot.polyweather.exchanges.clob_client import CLOBClient, MockCLOBClient
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
    convergence_exits: int = 0
    heartbeat_count: int = 0
    last_cycle_at: float = 0.0
    started_at: float = field(default_factory=time.time)
    last_error: str = ""


@dataclass
class OpenPaperPosition:
    """An open paper position held until ``closes_at``.

    The ``_final_outcome`` is sampled once at open time from ``p_realised``,
    then ``current_price`` walks from ``entry_price`` toward that outcome
    over the holding window. Mark-to-market unrealised P&L is reported each
    cycle; realised P&L only lands at settlement.
    """

    market_id: str
    event_id: str
    strategy: str
    station: str
    city: str
    side: str
    outcome: str
    token_id: str
    entry_price: Decimal
    current_price: Decimal
    size_tokens: Decimal
    size_usdc: Decimal
    p_model: float
    p_realised: float
    bucket_low: float
    bucket_high: float
    horizon_hours: float
    opened_at: float
    closes_at: float
    rebate_usdc: Decimal
    final_outcome: int
    fill_latency_seconds: float

    @property
    def unrealized_pnl(self) -> Decimal:
        return ((self.current_price - self.entry_price) * self.size_tokens).quantize(
            Decimal("0.0001")
        )


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
    # Middle-band price gate for the ensemble (wallet-research finding).
    price_band: dict[str, float] = field(default_factory=dict)
    target_cities: list[dict[str, Any]] = field(default_factory=list)
    bankroll_cap_usdc: Decimal | None = None
    first_24h_position_cap_usdc: Decimal = Decimal("5")
    # Pacing: only ``max_signals_per_cycle`` new positions open per cycle,
    # and the same bucket can't trade within ``bucket_cooldown_seconds`` of a
    # previous fill. Positions then sit OPEN for ``position_horizon_seconds``
    # in mock mode (simulating the time to market resolution) before they
    # settle to their binary outcome — giving the dashboard a smooth
    # mark-to-market curve instead of instant binary jumps.
    max_signals_per_event_per_cycle: int = 1
    max_signals_per_cycle: int = 1
    bucket_cooldown_seconds: float = 300.0
    # Negative-risk arb is a *basket* trade: every bucket in an event is one
    # leg. Without a guard the engine drains one leg per cycle for the same
    # event (Bug A — 8 Seoul arb fills in a minute). Once arb opens a leg on
    # an event we skip all further arb evaluation for that event until this
    # cooldown elapses. One hour by default; trimmed down for tests.
    arb_event_cooldown_seconds: float = 3600.0
    position_horizon_seconds: float = 60.0
    mtm_noise_pct: float = 0.03
    # Convergence-exit (brief §5.2.3 — "sell into the move"). LIVE-DATA only:
    # each cycle we reprice open positions to the real CLOB bid (the price we
    # could actually sell our outcome token into — an honest real mark, never
    # the pre-sampled outcome). When that bid has risen at least
    # ``convergence_exit_threshold`` above entry, we realise the gain now
    # instead of holding to binary resolution. Mock mode keeps the
    # entry+wiggle mark and never early-exits (no real book).
    convergence_exit_enabled: bool = True
    convergence_exit_threshold: float = 0.10
    # Mock-mode "true probability" = skill·p_model + (1-skill)·market_price.
    # 0.55 gives the bot a small real edge over the market, yielding
    # Sharpe ≈ 1–2 over many trades. 1.0 would reproduce the old
    # self-fulfilling outcome (Sharpe explodes), 0.5 = no edge.
    mock_model_skill: float = 0.55
    # live-paper mode: use real Polymarket Gamma + real forecast APIs
    # but DO NOT place orders on the exchange. Order placement stays
    # mocked so no real money is touched. Positions are still settled
    # on the engine's position_horizon_seconds — when we later add
    # real-resolution polling this becomes the gate for actual money.
    live_data: bool = False

    @classmethod
    def from_files(
        cls,
        risk_yaml: Path,
        markets_yaml: Path,
        weights_yaml: Path,
        *,
        mode: str = "paper",
        use_mock: bool = True,
        live_data: bool = False,
        cycle_seconds: float = 5.0,
        duration_seconds: float | None = None,
    ) -> EngineConfig:
        risk = WeatherRiskConfig.from_yaml(yaml.safe_load(Path(risk_yaml).read_text()))
        markets = yaml.safe_load(Path(markets_yaml).read_text())
        weights = yaml.safe_load(Path(weights_yaml).read_text())
        return cls(
            mode=mode,
            use_mock=use_mock,
            live_data=live_data,
            cycle_seconds=cycle_seconds,
            duration_seconds=duration_seconds,
            risk=risk,
            strategy_weights=dict(weights["weights"]),
            edge_thresholds_bps=dict(weights["edge_threshold_bps"]),
            confidence_minimums=dict(weights["confidence_minimum"]),
            price_band=dict(weights.get("price_band", {})),
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
        self.risk = WeatherRiskManager(
            config.risk, mode=config.mode, strategy_weights=config.strategy_weights
        )
        self.station_resolver = station_resolver or StationResolver()

        # Strategies
        self.s_ensemble = WeatherEnsembleStrategy(
            edge_threshold_bps=config.edge_thresholds_bps.get("weather_ensemble", 800),
            confidence_min=config.confidence_minimums.get("weather_ensemble", 0.55),
            kelly_multiplier=float(config.risk.kelly_fraction_multiplier),
            band_min=config.price_band.get("min", 0.15),
            band_max=config.price_band.get("max", 0.85),
            longshot_override_mult=config.price_band.get("longshot_override_mult", 3.0),
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
        #   use_mock=True  → all fixtures, no network at all
        #   live_data=True → real Polymarket + forecast APIs read-only,
        #                    execution still mocked (no real orders)
        #   neither        → fully live mode, exchange is None until
        #                    scripts/live_run.py initialises a real V2 client
        if config.use_mock:
            self.gamma = MockGammaClient()
            self.nws = MockNwsClient()
            self.open_meteo = MockOpenMeteoClient()
            self.met_office = MockMetOfficeClient()
            self.ncei = MockNceiBaseRateClient()
            self.exchange = MockPolymarketV2Client()
            self.clob = MockCLOBClient()
        elif config.live_data:
            self.gamma = GammaClient()
            try:
                self.nws = NwsClient()
            except RuntimeError as exc:
                logger.warning("nws_disabled", reason=str(exc))
                self.nws = MockNwsClient()
            self.open_meteo = OpenMeteoClient()
            try:
                self.met_office = MetOfficeClient()
            except RuntimeError as exc:
                logger.warning("met_office_disabled", reason=str(exc))
                self.met_office = MockMetOfficeClient()
            self.ncei = NceiBaseRateClient()
            # Critical: execution stays MOCKED in live-data paper mode.
            # No real orders are placed until scripts/live_run.py.
            self.exchange = MockPolymarketV2Client()
            self.clob = CLOBClient()
        else:
            self.gamma = GammaClient()
            self.nws = NwsClient()
            self.open_meteo = OpenMeteoClient()
            self.met_office = MetOfficeClient()
            self.ncei = NceiBaseRateClient()
            self.clob = CLOBClient()
            # Live exchange instantiated on demand by scripts/live_run.py
            self.exchange = None  # type: ignore[assignment]

        self.blender = EnsembleBlender()
        self._rng = random.Random(20260615)
        self._stop = asyncio.Event()
        # OPEN positions held until settlement — exposed to the dashboard
        self._open_positions: list[OpenPaperPosition] = []
        # market_id → last fill timestamp; honoured by ``_bucket_in_cooldown``
        self._last_fill_ts: dict[str, float] = {}
        # event_id → last timestamp an arb leg opened on that event; honoured
        # by ``_event_arb_in_cooldown`` so arb can't churn a basket leg-by-leg.
        self._last_event_arb_fired_ts: dict[str, float] = {}
        # Inject a deterministic equity tick at startup so the dashboard
        # always shows something.
        self.store.record_equity(
            self.risk.state.current_bankroll, Decimal("0"), Decimal("0")
        )

    @property
    def open_positions(self) -> list[OpenPaperPosition]:
        return list(self._open_positions)

    @property
    def unrealized_pnl_usdc(self) -> Decimal:
        return sum(
            (p.unrealized_pnl for p in self._open_positions),
            start=Decimal("0"),
        ).quantize(Decimal("0.0001"))

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

        # 1a. In live-data mode, poll Polymarket for any open position whose
        #     underlying market has just resolved. This overrides the
        #     horizon-timer settlement with the actual real outcome.
        if self.config.live_data and not self.config.use_mock:
            await self._check_real_resolutions(cycle_id)
            # 1a.2 Reprice open positions to the real CLOB bid and sell into
            #      any favorable convergence (brief §5.2.3) before the
            #      horizon-timer settlement runs.
            await self._reprice_open_positions(cycle_id)

        # 1b. Mark-to-market existing open positions; settle any whose horizon
        #     has elapsed. New realised P&L is recorded here.
        self._settle_open_positions(cycle_id)

        # 2. Pull markets and look for new opportunities — but cap how many
        #    new positions we open per cycle so the equity curve builds up
        #    smoothly from $0 P&L rather than jumping by $400 instantly.
        try:
            events = await self.gamma.list_active_weather_markets()
        except Exception as exc:  # noqa: BLE001
            logger.warning("gamma_fetch_failed", error=str(exc))
            self.metrics.last_error = str(exc)
            return

        self._cycle_signal_budget = self.config.max_signals_per_cycle
        for event in events:
            if self._cycle_signal_budget <= 0:
                break
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

        # 3. Equity tick — bankroll INCLUDING unrealised so the curve isn't
        #    flat while positions are open.
        marked_equity = (
            self.risk.state.current_bankroll + self.unrealized_pnl_usdc
        ).quantize(Decimal("0.0001"))
        self.store.record_equity(
            marked_equity,
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

    def _strategy_enabled(self, name: str) -> bool:
        """A strategy with weight <= 0 is disabled (skipped, not just capped).

        negative_risk_arb defaults to weight 0: it is a long-shot basket
        sprayer that produced 100% of the operator's losses, and the
        weather-wallet research shows the sub-15c tail bleeds. Bump its weight
        in strategy_weights.yaml to re-enable.
        """
        return self.config.strategy_weights.get(name, 0.0) > 0.0

    def _event_arb_in_cooldown(self, event_id: str) -> bool:
        """True if arb already opened a leg on this event recently.

        Prevents the negative-risk arb from firing one basket leg per cycle
        across many cycles for the same event (Bug A).
        """
        last = self._last_event_arb_fired_ts.get(event_id, 0.0)
        if last == 0.0:
            return False
        return (time.time() - last) < self.config.arb_event_cooldown_seconds

    async def _check_real_resolutions(self, cycle_id: str) -> None:
        """Settle any open paper position whose Polymarket market has closed.

        Strategy: fetch the current list of active weather events. Any of
        our open positions whose ``market_id`` is *not* in that list has
        either closed, archived, or fallen below volume threshold. We then
        look up the resolved outcome via a single Gamma read.
        """
        if not self._open_positions:
            return
        try:
            events = await self.gamma.list_active_weather_markets()
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolution_check_skipped", error=str(exc))
            return
        live_market_ids = {
            bucket.id
            for event in events
            for bucket in event.buckets
        }
        now = time.time()
        for pos in self._open_positions:
            if pos.market_id in live_market_ids:
                continue
            # Market no longer listed as active. In live mode we'd resolve
            # against the on-chain CTF outcome; for v1 we assume "resolved
            # YES" if the last CLOB midpoint we saw was > 0.5, else NO.
            try:
                mid = await self.clob.fetch_midpoint(pos.token_id)
            except Exception:  # noqa: BLE001
                mid = None
            if mid is None or mid <= 0:
                # Can't verify — leave the horizon timer to handle it.
                continue
            real_outcome = 1 if mid > 0.5 else 0
            if pos.final_outcome != real_outcome:
                logger.info(
                    "real_resolution_override",
                    market_id=pos.market_id,
                    horizon_outcome=pos.final_outcome,
                    real_outcome=real_outcome,
                    mid=mid,
                )
            pos.final_outcome = real_outcome
            pos.closes_at = now  # force immediate settlement in _settle_open_positions

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

        # Negative-risk arb on the basket. Disabled by default (weight 0 — see
        # _strategy_enabled). Also skip if arb already opened a leg on this
        # event within the cooldown window (Bug A).
        if self._strategy_enabled(self.s_arb.name) and not self._event_arb_in_cooldown(event.id):
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
            # In live-data mode, refresh price from the CLOB orderbook
            # rather than relying on Gamma's cached snapshot. CLOB returns
            # what the bot would have actually transacted against.
            best_bid, best_ask = bucket.best_bid, bucket.best_ask
            if self.config.live_data and not self.config.use_mock:
                book = await self.clob.fetch_book(bucket.token_id_yes)
                if book is not None and book.bids and book.asks:
                    best_bid = book.best_bid
                    best_ask = book.best_ask
            bucket_unit = getattr(bucket, "unit", "F")
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
                bucket_unit=bucket_unit,
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
                best_bid=best_bid,
                best_ask=best_ask,
                horizon_hours=horizon_h,
                forecast=forecast,
            )

            if self._strategy_enabled(self.s_ensemble.name):
                sig = self.s_ensemble.evaluate(view)
                if sig is not None:
                    edge = float(sig.metadata.get("edge_bps", 0.0))
                    candidates.append((edge, sig, forecast.p_bucket))

            if self._strategy_enabled(self.s_meanrev.name):
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
        for _edge_bps, sig, p_real in candidates:
            if fired >= self.config.max_signals_per_event_per_cycle:
                break
            if self._cycle_signal_budget <= 0:
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
            self._cycle_signal_budget -= 1

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

        ok, reason = self.risk.can_open(size_usdc, strategy=signal.strategy)
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

        # OPEN the position. Settlement happens later in
        # ``_settle_open_positions`` when the holding horizon elapses.
        self.risk.record_open(size_usdc, strategy=signal.strategy)
        # Paper-mode outcome draw: blend of model and market.
        # Applies in both --mock and --live-data modes; only fully-live
        # execution (real money) uses the actual market resolution.
        if self.config.use_mock or self.config.live_data:
            market_implied = float(target_price)
            skill = self.config.mock_model_skill
            true_p = skill * p_realised + (1.0 - skill) * market_implied
            true_p = max(0.0, min(1.0, true_p))
        else:
            true_p = p_realised
        final_outcome = 1 if self._rng.random() < true_p else 0
        rebate = (size_usdc * Decimal("0.0005")).quantize(Decimal("0.0001"))
        now = time.time()
        position = OpenPaperPosition(
            market_id=signal.market_id,
            event_id=event.id,
            strategy=signal.strategy,
            station=station.icao,
            city=station.city,
            side=signal.direction.value,
            outcome=signal.outcome,
            token_id=meta.get("token_id", ""),
            entry_price=target_price,
            current_price=target_price,
            size_tokens=size_tokens,
            size_usdc=size_usdc,
            p_model=p_model,
            p_realised=p_realised,
            bucket_low=float(meta.get("bucket_low", 0)),
            bucket_high=float(meta.get("bucket_high", 0)),
            horizon_hours=float(meta.get("horizon_hours", 0)),
            opened_at=now,
            closes_at=now + self.config.position_horizon_seconds,
            rebate_usdc=rebate,
            final_outcome=final_outcome,
            fill_latency_seconds=0.5 + self._rng.random() * 2.0,
        )
        self._open_positions.append(position)
        self.metrics.fills_total += 1  # treat OPEN as the fill event
        self.metrics.fills_by_strategy[signal.strategy] = (
            self.metrics.fills_by_strategy.get(signal.strategy, 0) + 1
        )
        # Cool the bucket so the same market isn't retraded next cycle.
        self._last_fill_ts[signal.market_id] = now
        # Arb is a basket trade — once one leg opens, lock the whole event so
        # the remaining legs aren't drained one-per-cycle (Bug A).
        if signal.strategy == self.s_arb.name:
            self._last_event_arb_fired_ts[event.id] = now

        self.store.record_decision(
            cycle_id=cycle_id,
            strategy=signal.strategy,
            market_id=signal.market_id,
            station=station.icao,
            city=station.city,
            decision="OPENED",
            reason=signal.reason,
            mid=float(target_price),
            confidence=signal.confidence,
            edge_bps=float(meta.get("edge_bps", 0.0)),
            model_probability=p_model,
            bucket_low=float(meta.get("bucket_low", 0)),
            bucket_high=float(meta.get("bucket_high", 0)),
            forecast_horizon_hours=float(meta.get("horizon_hours", 0)),
            extra={"size_usdc": str(size_usdc), "size_tokens": str(size_tokens)},
        )

    # ─── settlement of held positions ────────────────────────────────

    def _settle_open_positions(self, cycle_id: str) -> None:
        """Mark every open position to market; settle expired ones.

        Honest mark-to-market: a binary position is worth roughly what we paid
        for it until the market actually resolves — we do NOT know the outcome
        in advance, so the mark must NOT drift toward the pre-sampled 0/1
        outcome. (Doing so produced a fake unrealised spike: positions
        "destined to win" marked toward $1.00, inflating equity by +100% on
        cheap long-shots, then cratering at settlement.) Instead we mark at the
        entry price plus a small mean-zero wiggle, bounded by ``mtm_noise_pct``
        of entry. Realised P&L — the real binary payoff — only lands at
        settlement, giving a smooth, stepwise equity curve.
        """
        if not self._open_positions:
            return
        # In live-data mode the mark is the real CLOB bid set by
        # ``_reprice_open_positions``; don't overwrite it with the mock wiggle.
        live_marked = self.config.live_data and not self.config.use_mock
        now = time.time()
        still_open: list[OpenPaperPosition] = []
        for pos in self._open_positions:
            if not live_marked:
                wiggle = Decimal(
                    str((self._rng.random() - 0.5) * 2 * self.config.mtm_noise_pct)
                )
                mark = pos.entry_price * (Decimal("1") + wiggle)
                # Clamp to a sensible range
                if mark < Decimal("0.001"):
                    mark = Decimal("0.001")
                elif mark > Decimal("0.999"):
                    mark = Decimal("0.999")
                pos.current_price = mark.quantize(Decimal("0.0001"))

            if now < pos.closes_at:
                still_open.append(pos)
                continue

            # Settlement — realise the binary outcome.
            exit_price = Decimal("1.00") if pos.final_outcome == 1 else Decimal("0.00")
            self._close_position(
                pos,
                exit_price,
                now,
                cycle_id,
                exit_reason="horizon",
                decision_reason=f"horizon_elapsed outcome={pos.final_outcome}",
            )
        self._open_positions = still_open

    def _close_position(
        self,
        pos: OpenPaperPosition,
        exit_price: Decimal,
        now: float,
        cycle_id: str,
        *,
        exit_reason: str,
        decision_reason: str,
    ) -> Decimal:
        """Realise one position at ``exit_price``; record the trade + decision.

        Shared by horizon settlement (exit at the binary outcome 0/1) and
        convergence-exit (exit at the real CLOB bid). ``realised_outcome``
        always carries the pre-sampled binary outcome so the validation
        gate's Brier score stays a measure of *forecast* skill regardless of
        whether we sold early — P&L reflects the actual exit price.
        """
        exit_price = exit_price.quantize(Decimal("0.0001"))
        realised_pnl = (
            (exit_price - pos.entry_price) * pos.size_tokens + pos.rebate_usdc
        ).quantize(Decimal("0.0001"))
        trade = TradePair(
            market_id=pos.market_id,
            event_id=pos.event_id,
            strategy=pos.strategy,
            station=pos.station,
            city=pos.city,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            size=pos.size_tokens,
            fees_usdc=Decimal("0"),
            rebates_usdc=pos.rebate_usdc,
            realised_pnl_usdc=realised_pnl,
            opened_at=pos.opened_at,
            closed_at=now,
            fill_latency_seconds=pos.fill_latency_seconds,
            model_probability=pos.p_model,
            realised_outcome=pos.final_outcome,
            used_dynamic_fee=True,
            cap_violation=pos.size_usdc > self.risk.weather_position_cap_usdc(),
            metadata={
                "bucket_low": pos.bucket_low,
                "bucket_high": pos.bucket_high,
                "horizon_hours": pos.horizon_hours,
                "exit_reason": exit_reason,
            },
        )
        self.store.record_trade(trade)
        self.risk.record_close(pos.size_usdc, realised_pnl, strategy=pos.strategy)
        self.store.record_decision(
            cycle_id=cycle_id,
            strategy=pos.strategy,
            market_id=pos.market_id,
            station=pos.station,
            city=pos.city,
            decision="SETTLED",
            reason=decision_reason,
            mid=float(exit_price),
            confidence=None,
            edge_bps=None,
            model_probability=pos.p_model,
            bucket_low=pos.bucket_low,
            bucket_high=pos.bucket_high,
            forecast_horizon_hours=pos.horizon_hours,
            extra={
                "pnl_usdc": str(realised_pnl),
                "size_usdc": str(pos.size_usdc),
                "exit_reason": exit_reason,
            },
        )
        return realised_pnl

    async def _reprice_open_positions(self, cycle_id: str) -> None:
        """Reprice open positions to the real CLOB bid; sell into the move.

        Live-data only. The exit price for our long is the best bid we could
        sell the outcome token into — a real, honest mark (never the
        pre-sampled outcome). When that bid has risen at least
        ``convergence_exit_threshold`` above entry we realise the gain now
        ("sell into the move", brief §5.2.3) rather than holding to
        resolution. Positions below the threshold keep the refreshed mark and
        continue to their horizon / real-resolution settlement.
        """
        if not self._open_positions:
            return
        now = time.time()
        threshold = Decimal(str(self.config.convergence_exit_threshold))
        survivors: list[OpenPaperPosition] = []
        for pos in self._open_positions:
            book = None
            if pos.token_id:
                try:
                    book = await self.clob.fetch_book(pos.token_id)
                except Exception:  # noqa: BLE001
                    book = None
            if book is None or not book.bids:
                # No live book this cycle — keep the last mark, hold the position.
                survivors.append(pos)
                continue

            exit_bid = Decimal(str(book.best_bid)).quantize(Decimal("0.0001"))
            # Honest real mark: what we'd actually receive if we sold now.
            pos.current_price = exit_bid

            if self.config.convergence_exit_enabled and (exit_bid - pos.entry_price) >= threshold:
                self.metrics.convergence_exits += 1
                self._close_position(
                    pos,
                    exit_bid,
                    now,
                    cycle_id,
                    exit_reason="convergence_exit",
                    decision_reason=(
                        f"convergence_exit bid={exit_bid:.4f} entry={pos.entry_price:.4f} "
                        f"gain={(exit_bid - pos.entry_price):.4f}"
                    ),
                )
                continue
            survivors.append(pos)
        self._open_positions = survivors


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
