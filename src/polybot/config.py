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
    # Set to True to bypass the live-mode hard guard. NOT RECOMMENDED until
    # EIP-712 signing is implemented.
    allow_live: bool = False


class WalletConfig(BaseModel):
    private_key_env: str = "POLYMARKET_PRIVATE_KEY"
    api_key_env: str = "POLYMARKET_API_KEY"


class ScannerConfig(BaseModel):
    interval_seconds: int = 30
    min_volume_24h: float = 0
    min_liquidity: float = 10
    max_spread_pct: float = 30.0
    categories_allowlist: list[str] = Field(default_factory=list)
    categories_blocklist: list[str] = Field(default_factory=list)
    resolution_window_days: list[int] = Field(default_factory=lambda: [0, 7])
    btc_updown_only: bool = True
    btc_timeframes: list[str] = Field(
        default_factory=lambda: ["5 min", "15 min", "1 hour", "4 hour", "daily"]
    )


class StrategyItemConfig(BaseModel):
    name: str
    weight: float = 1.0
    params: dict[str, Any] = Field(default_factory=dict)


class AggregationConfig(BaseModel):
    min_confidence: float = 0.30
    conflict_resolution: str = "weighted_vote"
    strategy_weights: dict[str, float] = Field(default_factory=dict)
    # Lowered from 0.30 — single-strategy outputs at typical 0.5-0.7 confidence
    # multiplied by typical strategy weights of 0.30-0.45 produce net scores
    # in the 0.15-0.31 band. The previous default of 0.30 was silently
    # blocking single-strategy signals.
    min_net_score: float = 0.15


class StrategiesConfig(BaseModel):
    enabled: list[StrategyItemConfig] = Field(default_factory=list)
    aggregation: AggregationConfig = AggregationConfig()


class CircuitBreakerConfig(BaseModel):
    consecutive_losses_pause: int = 8
    consecutive_losses_size_reduction: float = 0.5
    api_errors_per_minute_pause: int = 15
    ws_disconnect_cancel_seconds: int = 30


class RiskConfig(BaseModel):
    max_position_size: float = 30
    max_portfolio_exposure: float = 150
    max_positions: int = 6
    max_daily_loss: float = 25
    min_trade_interval_seconds: int = 2
    max_order_size: float = 20
    max_slippage_pct: float = 5.0
    # Kelly sizing
    bankroll_usd: float = 500
    kelly_fraction: float = 0.50
    hard_cap_pct: float = 0.10
    edge_floor_bps: float = 3.0
    # NEW: explicit min_usd field — was previously hidden as a getattr-default.
    # Setting this in YAML now actually works.
    min_usd: float = 2.0
    # ATH drawdown kill — 0.0 disables (default). Percent expressed as a
    # fraction (0.40 = 40%). When equity falls this far from the watermark
    # the bot halts permanently until manually restarted.
    ath_drawdown_kill_pct: float = 0.0
    daily_loss_stop_pct: float = 0.0  # 0 disables; e.g. 0.10 for 10%
    per_timeframe_cap_pct: dict[str, float] = Field(
        default_factory=lambda: {
            "5m": 0.04,
            "15m": 0.05,
            "1h": 0.05,
            "4h": 0.06,
            "daily": 0.06,
        }
    )
    circuit_breakers: CircuitBreakerConfig = CircuitBreakerConfig()


class ExecutionConfig(BaseModel):
    rate_limit_per_second: int = 5
    order_ttl_seconds: int = 15
    retry_attempts: int = 1
    retry_backoff_seconds: list[float] = Field(default_factory=lambda: [1.0])
    # Trading-loop tuning
    loop_interval_ms: int = 500
    max_trades_per_cycle: int = 6
    max_trades_per_market_per_cycle: int = 1
    cooldown_per_market: bool = True
    high_conf_override_after_s: float = 1.0
    high_conf_override_threshold: float = 0.85
    auto_close_before_expiry_s: int = 20
    # Safety net: positions stuck open beyond this duration are force-exited
    # by PositionManager so held-to-expiry strategies on long markets can't
    # permanently lock the global position cap. Set to 0 to disable.
    max_position_hold_seconds: float = 3600.0


class AlertsConfig(BaseModel):
    discord_webhook_env: str = "DISCORD_WEBHOOK_URL"
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: str = "TELEGRAM_CHAT_ID"


class MonitoringConfig(BaseModel):
    metrics_port: int = 9090
    alerts: AlertsConfig = AlertsConfig()
    daily_summary_hour: int = 18


class DecisionLogConfig(BaseModel):
    """Structured decision log configuration."""
    path: str = "logs/decisions.jsonl"
    flush_every: int = 25


class FeaturesRetentionConfig(BaseModel):
    """Settings for periodic features.db cleanup."""
    enabled: bool = True
    keep_days: int = 30
    cleanup_interval_hours: int = 24


class ClobV2Config(BaseModel):
    """CLOB V2 SDK + API surface settings.

    The V2 migration is non-negotiable for live trading (V1 endpoints
    were sunset 28 Apr 2026), but paper mode still runs against the
    public Gamma / CLOB read endpoints and ignores these.
    """

    version: int = 2
    host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    collateral: str = "pUSD"
    fee_rate_cache_ttl_s: float = 30.0
    batch_order_limit: int = 15


class MakerConfig(BaseModel):
    """Two-sided maker-quoting strategy settings.

    Default `enabled: False` so the v1 paper loop keeps running until
    the maker pivot is explicitly turned on in config.
    """

    enabled: bool = False
    primary_markets: list[str] = Field(default_factory=lambda: ["btc-5m"])
    min_half_spread_cents: float = 2.0
    adverse_selection_buffer_cents: float = 1.0
    requote_threshold_cents: float = 1.5
    max_quote_lifetime_s: float = 30.0
    flatten_before_expiry_s: float = 10.0
    inventory_skew_factor: float = 0.5
    target_fill_rate_pct: float = 30.0
    target_size_shares: float = 5.0

    # Inventory / flatten thresholds
    max_inventory_per_side: float = 200.0
    delta_threshold_uncertain: float = 0.005
    delta_threshold_directional: float = 0.15
    directional_bet_price: float = 0.95

    # Feed-health timeouts (seconds without a tick → cancel-all)
    binance_stale_threshold_s: float = 2.0
    clob_ws_reconnect_timeout_s: float = 5.0

    # Latency auto-disable thresholds
    latency_warn_p95_ms: float = 150.0
    latency_kill_p95_ms: float = 200.0
    latency_sustained_breach_s: float = 60.0


class DeploymentConfig(BaseModel):
    mode: str = "paper"        # paper | live (mirrors bot.mode but explicit)
    vps_region: str = ""        # informational
    docker: bool = False
    heartbeat_interval_s: float = 3600.0


class Config(BaseModel):
    bot: BotConfig = BotConfig()
    wallet: WalletConfig = WalletConfig()
    scanner: ScannerConfig = ScannerConfig()
    strategies: StrategiesConfig = StrategiesConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    decision_log: DecisionLogConfig = DecisionLogConfig()
    features_retention: FeaturesRetentionConfig = FeaturesRetentionConfig()
    clob: ClobV2Config = ClobV2Config()
    maker: MakerConfig = MakerConfig()
    deployment: DeploymentConfig = DeploymentConfig()

    @property
    def is_live(self) -> bool:
        return self.bot.mode == "live"

    model_config = {"extra": "ignore"}


def load_config(path: str = "config.yaml") -> Config:
    """Load configuration from a YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        return Config()

    return Config(**raw)
