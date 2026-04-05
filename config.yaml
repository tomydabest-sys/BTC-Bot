bot:
  name: "btc-bot"
  mode: "paper"
  log_level: "INFO"
  data_dir: "./data"

wallet:
  private_key_env: "POLYMARKET_PRIVATE_KEY"
  api_key_env: "POLYMARKET_API_KEY"

scanner:
  interval_seconds: 30
  min_volume_24h: 0
  min_liquidity: 10
  max_spread_pct: 20.0
  categories_allowlist: []
  categories_blocklist: []
  resolution_window_days: [0, 1]
  btc_updown_only: true
  btc_timeframes: ["5 min", "15 min", "1 hour", "4 hour"]

strategies:
  enabled:
    # PRIMARY: Sub-second latency arb
    # Binance WS ticks → detect 0.05% BTC move → buy before Polymarket adjusts
    - name: "latency_arb"
      weight: 1.0
      params:
        min_gap_pct: 0.015
        max_gap_pct: 0.20
        min_exchange_move_pct: 0.003
        fee_buffer_pct: 0.005
        size_pct: 0.04
        confidence_floor: 0.55
        exchange_symbol: "BTC"

    # SECONDARY: Momentum lag (1-5s trend continuation)
    - name: "momentum_lag"
      weight: 1.0
      params:
        min_move_30s_pct: 0.003
        min_move_60s_pct: 0.006
        min_gap_pct: 0.015
        max_gap_pct: 0.15
        thin_book_threshold: 0.015
        size_pct: 0.04
        exchange_symbol: "BTC"

    # TERTIARY: Dual direction arb (free money if Up+Down < $1)
    - name: "dual_direction_arb"
      weight: 1.0
      params:
        min_profit_pct: 0.01
        max_total_cost: 0.99
        min_liquidity_each_side: 10.0
        size_pct: 0.06

  aggregation:
    min_confidence: 0.55
    conflict_resolution: "highest_confidence"

risk:
  max_position_size: 30
  max_portfolio_exposure: 150
  max_positions: 5
  max_daily_loss: 25
  min_trade_interval_seconds: 8
  max_order_size: 20
  max_slippage_pct: 5.0
  circuit_breakers:
    consecutive_losses_pause: 6
    consecutive_losses_size_reduction: 0.5
    api_errors_per_minute_pause: 15
    ws_disconnect_cancel_seconds: 30

execution:
  rate_limit_per_second: 5
  order_ttl_seconds: 15
  retry_attempts: 1
  retry_backoff_seconds: [1]

monitoring:
  metrics_port: 9090
  alerts:
    discord_webhook_env: "DISCORD_WEBHOOK_URL"
    telegram_bot_token_env: "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: "TELEGRAM_CHAT_ID"
  daily_summary_hour: 18
