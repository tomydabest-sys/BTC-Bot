"""Weather-mode risk module: position caps, kill switch, day-1 live override.

Three distinct halt regimes (per the v1 spec):

  - ATH drawdown ≥ 20% → ``ath_killed`` permanently latched; only manual
    ``reset_ath_kill()`` clears it.
  - Daily loss ≥ max_daily_loss_usdc → 24h cooldown.
  - Consecutive losses ≥ N → pause for ``consecutive_loss_pause_seconds``
    (default 30 min in live, configurable down for mock).

Each pause records the timestamp it started; ``check_kill_switch`` auto-
clears expired pauses so the dashboard isn't stuck in HALTED forever.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class WeatherRiskConfig:
    bankroll_usdc: Decimal = Decimal("1260")
    max_position_size_usdc: Decimal = Decimal("12")
    max_total_open_exposure_usdc: Decimal = Decimal("504")
    max_daily_loss_usdc: Decimal = Decimal("50")
    max_weekly_loss_usdc: Decimal = Decimal("150")
    kelly_fraction_multiplier: Decimal = Decimal("0.25")
    position_minimum_usdc: Decimal = Decimal("1.5")
    first_24h_live_position_cap_usdc: Decimal = Decimal("5")
    all_time_high_kill_drawdown_pct: Decimal = Decimal("0.20")
    consecutive_loss_pause_count: int = 5
    # Auto-recovery windows (seconds). Per the v1 spec.
    consecutive_loss_pause_seconds: float = 1800.0   # 30 min
    daily_loss_cooldown_seconds: float = 86400.0     # 24 h

    @classmethod
    def from_yaml(cls, data: dict) -> WeatherRiskConfig:
        return cls(
            bankroll_usdc=Decimal(str(data["bankroll_usdc"])),
            max_position_size_usdc=Decimal(str(data["max_position_size_usdc"])),
            max_total_open_exposure_usdc=Decimal(str(data["max_total_open_exposure_usdc"])),
            max_daily_loss_usdc=Decimal(str(data["max_daily_loss_usdc"])),
            max_weekly_loss_usdc=Decimal(str(data["max_weekly_loss_usdc"])),
            kelly_fraction_multiplier=Decimal(str(data["kelly_fraction_multiplier"])),
            position_minimum_usdc=Decimal(str(data["position_minimum_usdc"])),
            first_24h_live_position_cap_usdc=Decimal(str(data["first_24h_live_position_cap_usdc"])),
            all_time_high_kill_drawdown_pct=Decimal(str(data["all_time_high_kill_drawdown_pct"])),
            consecutive_loss_pause_count=int(data["consecutive_loss_pause_count"]),
            consecutive_loss_pause_seconds=float(
                data.get("consecutive_loss_pause_seconds", 1800.0)
            ),
            daily_loss_cooldown_seconds=float(
                data.get("daily_loss_cooldown_seconds", 86400.0)
            ),
        )


@dataclass
class WeatherRiskState:
    current_bankroll: Decimal = Decimal("1260")
    ath_bankroll: Decimal = Decimal("1260")
    open_exposure: Decimal = Decimal("0")
    open_exposure_by_strategy: dict[str, Decimal] = field(default_factory=dict)
    daily_pnl: Decimal = Decimal("0")
    weekly_pnl: Decimal = Decimal("0")
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""
    halt_started_ts: float = 0.0
    halt_kind: str = ""              # "ath" | "daily" | "consecutive"
    ath_killed: bool = False         # latched permanently on ATH breach
    live_session_start_ts: float = 0.0


class WeatherRiskManager:
    """All-Decimal money math; floats only enter via probabilities."""

    def __init__(
        self,
        config: WeatherRiskConfig,
        mode: str = "paper",
        strategy_weights: dict[str, float] | None = None,
    ) -> None:
        self.config = config
        self.mode = mode
        self.state = WeatherRiskState(
            current_bankroll=config.bankroll_usdc,
            ath_bankroll=config.bankroll_usdc,
        )
        # First-24h-live override is hard-coded — bot caps individual positions
        # at $5 for the first 24h regardless of config (per the prompt).
        self._hard_first_24h_cap = Decimal("5")
        # Per-strategy concentration: each strategy's open exposure is capped at
        # ``weight × current_bankroll`` (e.g. negative_risk_arb at 0.20 can hold
        # at most ~$252 of $1260). Weights come from strategy_weights.yaml; a
        # strategy with no weight (or no weights configured) is uncapped here
        # and only bounded by the global exposure cap.
        self.strategy_weights: dict[str, Decimal] = {
            str(k): Decimal(str(v)) for k, v in (strategy_weights or {}).items()
        }

    # ─── caps ────────────────────────────────────────────────────────

    def live_session_age_hours(self) -> float:
        if self.state.live_session_start_ts <= 0:
            return 0.0
        return (time.time() - self.state.live_session_start_ts) / 3600.0

    def weather_position_cap_usdc(self) -> Decimal:
        base_cap = self.state.current_bankroll * Decimal("0.01")
        if self.mode == "live" and self.live_session_age_hours() < 24:
            return min(base_cap, self._hard_first_24h_cap)
        return min(base_cap, self.config.max_position_size_usdc)

    def quarter_kelly_size(
        self,
        p_win: float,
        target_price: Decimal,
    ) -> Decimal:
        if target_price <= 0 or target_price >= 1:
            return Decimal("0")
        b_float = float((Decimal("1") - target_price) / target_price)
        f = (p_win * (b_float + 1) - 1) / b_float
        if f <= 0:
            return Decimal("0")
        f *= float(self.config.kelly_fraction_multiplier)
        # Fat-tail guard (Bug B): standard Kelly will happily bet the full 1%
        # cap on a long-shot price where the model claims a huge edge — e.g. an
        # $0.001 token at "800 bps over model". Such a bet loses ~100% of its
        # stake with ~99.9% probability; eight of them in a row drained ~$96.
        # Scale the cap down linearly below $0.05 so tail bets can't reach the
        # full position cap. At $0.05+ the discount is 1.0 (no change). Prices
        # whose discounted cap falls below ``position_minimum_usdc`` size to 0,
        # so the bot abstains from the tiniest long-shots entirely.
        tail_discount = min(Decimal("1"), target_price / Decimal("0.05"))
        cap = (self.weather_position_cap_usdc() * tail_discount).quantize(Decimal("0.0001"))
        sized = self.state.current_bankroll * Decimal(str(f))
        sized = sized.quantize(Decimal("0.0001"))
        sized = min(sized, cap)
        if sized < self.config.position_minimum_usdc:
            return Decimal("0")
        return sized

    # ─── kill switches ───────────────────────────────────────────────

    def check_kill_switch(self) -> tuple[bool, str]:  # noqa: C901
        # ATH drawdown is the only permanent halt — never auto-recover.
        if self.state.ath_killed:
            return True, self.state.halt_reason

        # ATH check fires first because it's the strongest signal.
        if self.state.ath_bankroll > 0:
            dd = (self.state.ath_bankroll - self.state.current_bankroll) / self.state.ath_bankroll
            if dd >= self.config.all_time_high_kill_drawdown_pct:
                return self._latch_ath_kill(
                    f"ath_drawdown_kill_switch: {dd:.4f} "
                    f">= {self.config.all_time_high_kill_drawdown_pct}"
                )

        # Daily-loss cooldown — auto-resume after window.
        if self.state.halted and self.state.halt_kind == "daily":
            if self._cooldown_expired(self.config.daily_loss_cooldown_seconds):
                self._clear_halt()
                self.state.daily_pnl = Decimal("0")
            else:
                return True, self.state.halt_reason
        if self.state.daily_pnl <= -self.config.max_daily_loss_usdc:
            return self._open_halt(
                "daily",
                f"daily_loss_kill_switch: {self.state.daily_pnl} "
                f"<= -{self.config.max_daily_loss_usdc}",
            )

        # Consecutive-loss pause — auto-resume after window.
        if self.state.halted and self.state.halt_kind == "consecutive":
            if self._cooldown_expired(self.config.consecutive_loss_pause_seconds):
                self._clear_halt()
                self.state.consecutive_losses = 0
            else:
                return True, self.state.halt_reason
        if self.state.consecutive_losses >= self.config.consecutive_loss_pause_count:
            return self._open_halt(
                "consecutive",
                f"consecutive_loss_pause: {self.state.consecutive_losses} losses in a row",
            )

        return False, ""

    # ─── halt helpers ────────────────────────────────────────────────

    def _open_halt(self, kind: str, reason: str) -> tuple[bool, str]:
        self.state.halted = True
        self.state.halt_kind = kind
        self.state.halt_reason = reason
        if self.state.halt_started_ts == 0:
            self.state.halt_started_ts = time.time()
        return True, reason

    def _latch_ath_kill(self, reason: str) -> tuple[bool, str]:
        self.state.ath_killed = True
        return self._open_halt("ath", reason)

    def _clear_halt(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.halt_kind = ""
        self.state.halt_started_ts = 0.0

    def _cooldown_expired(self, window_s: float) -> bool:
        if self.state.halt_started_ts <= 0:
            return False
        return (time.time() - self.state.halt_started_ts) >= window_s

    def halt_cooldown_seconds(self) -> float | None:
        """The cooldown window for the current halt kind, or None.

        ATH kills are permanent (no auto-recovery) so they return None.
        """
        if self.state.halt_kind == "daily":
            return self.config.daily_loss_cooldown_seconds
        if self.state.halt_kind == "consecutive":
            return self.config.consecutive_loss_pause_seconds
        return None

    def halt_recovery_in_seconds(self) -> float | None:
        """Seconds until an auto-recovering halt lifts.

        ``None`` when not halted or when the halt is permanent (ATH kill), so
        the dashboard can render "permanent — manual reset" vs a countdown.
        Clamped at 0 so an expired-but-not-yet-cleared halt reads "0s".
        """
        if not self.state.halted or self.state.halt_started_ts <= 0:
            return None
        window = self.halt_cooldown_seconds()
        if window is None:
            return None
        return max(0.0, (self.state.halt_started_ts + window) - time.time())

    def reset_ath_kill(self) -> None:
        """Operator-only manual reset after an ATH drawdown halt."""
        self.state.ath_killed = False
        self._clear_halt()
        self.state.ath_bankroll = self.state.current_bankroll

    def strategy_exposure_cap_usdc(self, strategy: str) -> Decimal | None:
        """Max open exposure for ``strategy`` = weight × current bankroll.

        ``None`` when the strategy has no configured weight (uncapped here).
        """
        weight = self.strategy_weights.get(strategy)
        if weight is None:
            return None
        return (self.state.current_bankroll * weight).quantize(Decimal("0.0001"))

    def strategy_open_exposure_usdc(self, strategy: str) -> Decimal:
        return self.state.open_exposure_by_strategy.get(strategy, Decimal("0"))

    def can_open(self, notional_usdc: Decimal, strategy: str | None = None) -> tuple[bool, str]:
        halted, reason = self.check_kill_switch()
        if halted:
            return False, reason
        cap = self.weather_position_cap_usdc()
        if notional_usdc > cap:
            return False, f"position_cap_exceeded {notional_usdc} > {cap}"
        projected = self.state.open_exposure + notional_usdc
        if projected > self.config.max_total_open_exposure_usdc:
            return False, (
                f"total_exposure_cap {projected} > {self.config.max_total_open_exposure_usdc}"
            )
        # Per-strategy concentration cap (Bug D): one strategy can't soak up the
        # whole bankroll. ``negative_risk_arb`` at weight 0.20 tops out at ~$252.
        if strategy is not None:
            strat_cap = self.strategy_exposure_cap_usdc(strategy)
            if strat_cap is not None:
                strat_projected = self.strategy_open_exposure_usdc(strategy) + notional_usdc
                if strat_projected > strat_cap:
                    return False, (
                        f"strategy_exposure_cap:{strategy} {strat_projected} > {strat_cap}"
                    )
        return True, "ok"

    # ─── bookkeeping ─────────────────────────────────────────────────

    def record_open(self, notional_usdc: Decimal, strategy: str | None = None) -> None:
        self.state.open_exposure += notional_usdc
        if strategy is not None:
            self.state.open_exposure_by_strategy[strategy] = (
                self.strategy_open_exposure_usdc(strategy) + notional_usdc
            )

    def record_close(
        self, notional_usdc: Decimal, pnl_usdc: Decimal, strategy: str | None = None
    ) -> None:
        self.state.open_exposure = max(Decimal("0"), self.state.open_exposure - notional_usdc)
        if strategy is not None:
            remaining = self.strategy_open_exposure_usdc(strategy) - notional_usdc
            self.state.open_exposure_by_strategy[strategy] = max(Decimal("0"), remaining)
        self.state.daily_pnl += pnl_usdc
        self.state.weekly_pnl += pnl_usdc
        self.state.current_bankroll += pnl_usdc
        if self.state.current_bankroll > self.state.ath_bankroll:
            self.state.ath_bankroll = self.state.current_bankroll
        if pnl_usdc < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

    def begin_live_session(self) -> None:
        self.state.live_session_start_ts = time.time()
