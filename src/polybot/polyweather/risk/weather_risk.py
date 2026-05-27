"""Weather-mode risk module: position caps, kill switch, day-1 live override."""

from __future__ import annotations

import time
from dataclasses import dataclass
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
        )


@dataclass
class WeatherRiskState:
    current_bankroll: Decimal = Decimal("1260")
    ath_bankroll: Decimal = Decimal("1260")
    open_exposure: Decimal = Decimal("0")
    daily_pnl: Decimal = Decimal("0")
    weekly_pnl: Decimal = Decimal("0")
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""
    live_session_start_ts: float = 0.0


class WeatherRiskManager:
    """All-Decimal money math; floats only enter via probabilities."""

    def __init__(self, config: WeatherRiskConfig, mode: str = "paper") -> None:
        self.config = config
        self.mode = mode
        self.state = WeatherRiskState(
            current_bankroll=config.bankroll_usdc,
            ath_bankroll=config.bankroll_usdc,
        )
        # First-24h-live override is hard-coded — bot caps individual positions
        # at $5 for the first 24h regardless of config (per the prompt).
        self._hard_first_24h_cap = Decimal("5")

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
        cap = self.weather_position_cap_usdc()
        sized = self.state.current_bankroll * Decimal(str(f))
        sized = sized.quantize(Decimal("0.0001"))
        sized = min(sized, cap)
        if sized < self.config.position_minimum_usdc:
            return Decimal("0")
        return sized

    # ─── kill switches ───────────────────────────────────────────────

    def check_kill_switch(self) -> tuple[bool, str]:
        if self.state.halted:
            return True, self.state.halt_reason
        if self.state.daily_pnl <= -self.config.max_daily_loss_usdc:
            return self._halt(
                f"daily_loss_kill_switch: {self.state.daily_pnl} "
                f"<= -{self.config.max_daily_loss_usdc}"
            )
        if self.state.ath_bankroll > 0:
            dd = (self.state.ath_bankroll - self.state.current_bankroll) / self.state.ath_bankroll
            if dd >= self.config.all_time_high_kill_drawdown_pct:
                return self._halt(
                    f"ath_drawdown_kill_switch: {dd:.4f} "
                    f">= {self.config.all_time_high_kill_drawdown_pct}"
                )
        if self.state.consecutive_losses >= self.config.consecutive_loss_pause_count:
            return self._halt(
                f"consecutive_loss_pause: {self.state.consecutive_losses} losses in a row"
            )
        return False, ""

    def _halt(self, reason: str) -> tuple[bool, str]:
        self.state.halted = True
        self.state.halt_reason = reason
        return True, reason

    def can_open(self, notional_usdc: Decimal) -> tuple[bool, str]:
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
        return True, "ok"

    # ─── bookkeeping ─────────────────────────────────────────────────

    def record_open(self, notional_usdc: Decimal) -> None:
        self.state.open_exposure += notional_usdc

    def record_close(self, notional_usdc: Decimal, pnl_usdc: Decimal) -> None:
        self.state.open_exposure = max(Decimal("0"), self.state.open_exposure - notional_usdc)
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
