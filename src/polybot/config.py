"""Configuration loading and validation via Pydantic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class BotConfig(BaseModel):
    name: str = "polymarket-bot"
    mode: str = "paper"
    log_level: str = "INFO"
    data_dir: str = "./data"


class WalletConfig(BaseModel):
    private_key_env: str = "POLYMARKET_PRIVATE_KEY"
    api_key_env: str = "POLYMARKET_API_KEY"


class ScannerConfig(BaseModel):
    interval_seconds: int = 300
    min_volume_24h: float = 10000
    min_liquidity: float = 5000
    max_spread_pct: float = 5.0
    categories_allowlist: list[str] = Field(default_factory=list)
    categories_blocklist: list[str] = Field(default_factory=list)
    resolution_window_days: list[int] = Field(default_factory=lambda: [1, 30])
    btc_updown_only: bool = True
    btc_timeframes: list[str] = Field(
        default_factory=lambda: ["5 min", "15 min", "1 hour", "4 hour"]
    )


class StrategyItemConfig(BaseModel):
    name: str
    weight: float = 1.0
    params: dict[str, Any] = Field(default_factory=dict)


class AggregationConfig(BaseModel):
    min_confidence: float = 0.5
    conflict_resolution: str = "skip"


class StrategiesConfig(BaseModel):
    enabled: list[StrategyItemConfig] = Field(default_factory=list)
    aggregation: AggregationConfig = AggregationConfig()


class CircuitBreakerConfig(BaseModel):
    consecutive_losses_pause: int = 3
    consecutive_losses_size_reduction: float = 0.5
    api_errors_per_minute_pause: int = 5
    ws_disconnect_cancel_seconds: int = 120


class RiskConfig(BaseModel):
    max_position_size: float = 500
    max_portfolio_exposure: float = 5000
    max_positions: int = 10
    max_daily_loss: float = 250
    min_trade_interval_seconds: int = 30
    max_order_size: float = 200
    max_slippage_pct: float = 2.0
    circuit_breakers: CircuitBreakerConfig = CircuitBreakerConfig()


class ExecutionConfig(BaseModel):
    rate_limit_per_second: int = 5
    order_ttl_seconds: int = 300
    retry_attempts: int = 3
    retry_backoff_seconds: list[int] = Field(default_factory=lambda: [1, 2, 4])


class AlertsConfig(BaseModel):
    discord_webhook_env: str = "DISCORD_WEBHOOK_URL"
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: str = "TELEGRAM_CHAT_ID"


class MonitoringConfig(BaseModel):
    metrics_port: int = 9090
    alerts: AlertsConfig = AlertsConfig()
    daily_summary_hour: int = 18


class Config(BaseModel):
    bot: BotConfig = BotConfig()
    wallet: WalletConfig = WalletConfig()
    scanner: ScannerConfig = ScannerConfig()
    strategies: StrategiesConfig = StrategiesConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    monitoring: MonitoringConfig = MonitoringConfig()

    @property
    def is_live(self) -> bool:
        return self.bot.mode == "live"


def load_config(path: str = "config.yaml") -> Config:
    """Load configuration from a YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if raw is None:
        return Config()

    return Config(**raw)
