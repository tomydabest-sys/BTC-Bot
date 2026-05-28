"""Engine-level guard for Bug A: negative-risk arb basket spam.

Negative-risk arb is a *basket* trade — every bucket of an event is one leg.
Before the fix the engine drained one leg per cycle for the same event (the
operator saw 8 Seoul arb fills inside a minute, each a -$11.99 loser). The
engine now records the event id when an arb leg opens and skips all further
arb evaluation for that event until ``arb_event_cooldown_seconds`` elapses.

These tests drive ``_evaluate_event`` directly with a hand-built event whose
ask prices sum well below 1.0 (so arb fires with a dominant edge). The two
single-signal strategies are silenced so arb deterministically wins the
per-event slot, isolating the cooldown behaviour under test.
"""

from __future__ import annotations

import time

import pytest

from polybot.polyweather.data.stations.station_resolver import ResolvedStation
from polybot.polyweather.exchanges.gamma_client import WeatherBucket, WeatherEvent

ARB_STRATEGY = "negative_risk_arb"


class _NoSignal:
    """Stand-in strategy that never produces a candidate."""

    def evaluate(self, *_args, **_kwargs):
        return None


def _arb_event() -> WeatherEvent:
    # Five buckets, asks summing to 0.25 → sum_asks << 0.98 → arb fires with a
    # ~7500 bps gap, far above anything the ensemble could produce here.
    buckets = [
        WeatherBucket(
            id=f"arb_b{i}",
            token_id_yes=f"yes{i}",
            token_id_no=f"no{i}",
            bucket_low=float(60 + i * 5),
            bucket_high=float(65 + i * 5),
            best_bid=0.03,
            best_ask=0.05,
            volume_24hr=10_000.0,
            question=f"High temp {60 + i * 5}-{65 + i * 5}F?",
            unit="F",
        )
        for i in range(5)
    ]
    return WeatherEvent(
        id="evt_arb_test",
        slug="arb-test",
        title="Arb test event",
        category="weather",
        tags=["weather"],
        end_date="2030-01-01T00:00:00Z",
        volume_24hr=50_000.0,
        active=True,
        closed=False,
        rules="High temperature at KLGA airport on the settlement date.",
        buckets=buckets,
    )


def _station() -> ResolvedStation:
    return ResolvedStation(
        icao="KLGA",
        city="New York",
        source="NWS",
        lat=40.7769,
        lon=-73.8740,
        confidence=1.0,
        matched_alias="KLGA",
    )


@pytest.mark.asyncio
async def test_arb_fires_once_then_event_cooldown_blocks_it(engine_factory):
    engine, _store = engine_factory(max_signals_per_cycle=5)
    # Silence the competing strategies so arb owns the per-event slot.
    engine.s_ensemble = _NoSignal()
    engine.s_meanrev = _NoSignal()
    engine.config.arb_event_cooldown_seconds = 3600.0

    event, station = _arb_event(), _station()

    # Cycle 1: arb should open exactly one leg and arm the event cooldown.
    engine._cycle_signal_budget = 100
    await engine._evaluate_event("00001", event, station)
    assert engine.metrics.fills_by_strategy.get(ARB_STRATEGY, 0) == 1
    assert event.id in engine._last_event_arb_fired_ts

    # Cycles 2-4: the whole event is in arb cooldown, so no further legs open
    # even though four buckets are still untouched and would otherwise fire.
    for cid in ("00002", "00003", "00004"):
        engine._cycle_signal_budget = 100
        await engine._evaluate_event(cid, event, station)
    assert engine.metrics.fills_by_strategy.get(ARB_STRATEGY, 0) == 1


@pytest.mark.asyncio
async def test_arb_resumes_after_event_cooldown_expires(engine_factory):
    engine, _store = engine_factory(max_signals_per_cycle=5)
    engine.s_ensemble = _NoSignal()
    engine.s_meanrev = _NoSignal()
    engine.config.arb_event_cooldown_seconds = 3600.0
    # The first leg shouldn't be re-blocked by its own per-bucket cooldown.
    engine.config.bucket_cooldown_seconds = 0.0

    event, station = _arb_event(), _station()

    engine._cycle_signal_budget = 100
    await engine._evaluate_event("00001", event, station)
    assert engine.metrics.fills_by_strategy.get(ARB_STRATEGY, 0) == 1

    # Pretend the arb leg opened well in the past → cooldown has elapsed.
    engine._last_event_arb_fired_ts[event.id] = time.time() - 7200.0

    engine._cycle_signal_budget = 100
    await engine._evaluate_event("00002", event, station)
    assert engine.metrics.fills_by_strategy.get(ARB_STRATEGY, 0) == 2


def test_event_arb_cooldown_helper(engine_factory):
    engine, _store = engine_factory()
    engine.config.arb_event_cooldown_seconds = 3600.0
    assert engine._event_arb_in_cooldown("never_fired") is False
    engine._last_event_arb_fired_ts["evt_x"] = time.time()
    assert engine._event_arb_in_cooldown("evt_x") is True
    engine._last_event_arb_fired_ts["evt_x"] = time.time() - 7200.0
    assert engine._event_arb_in_cooldown("evt_x") is False
