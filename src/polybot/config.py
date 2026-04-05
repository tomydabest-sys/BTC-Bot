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
  min_liquidity: 50
  max_spread_pct: 15.0
  categories_allowlist: []
  categories_blocklist: []
  resolution_window_days: [0, 1]

strategies:
  enabled:
    # Start with just 2 strategies until you validate they work.
    # Monte Carlo was firing on every market every cycle — disabled for now.

    - name: "latency_arb"
      weight: 1.0
      params:
        min_gap_pct: 0.05
        max_gap_pct: 0.15
        min_exchange_move_pct: 0.03
        fee_buffer_pct: 0.02
        size_pct: 0.02
        exchange_symbol: "BTC"

    - name: "momentum_lag"
      weight: 1.0
      params:
        min_move_30s_pct: 0.02
        min_move_60s_pct: 0.035
        min_gap_pct: 0.05
        thin_book_threshold: 0.05
        size_pct: 0.02
        exchange_symbol: "BTC"

    - name: "dual_direction_arb"
      weight: 1.0
      params:
        min_profit_pct: 0.02
        max_total_cost: 0.98
        min_liquidity_each_side: 50.0
        size_pct: 0.03

    - name: "market_maker"
      weight: 0.5
      params:
        min_spread: 0.04
        target_spread: 0.05
        max_inventory_pct: 0.10
        skew_factor: 0.5
        size_pct: 0.01

  aggregation:
    min_confidence: 0.6
    conflict_resolution: "skip"

risk:
  max_position_size: 25
  max_portfolio_exposure: 100
  max_positions: 3
  max_daily_loss: 15
  min_trade_interval_seconds: 60
  max_order_size: 15
  max_slippage_pct: 3.0
  circuit_breakers:
    consecutive_losses_pause: 3
    consecutive_losses_size_reduction: 0.5
    api_errors_per_minute_pause: 5
    ws_disconnect_cancel_seconds: 120

execution:
  rate_limit_per_second: 5
  order_ttl_seconds: 120
  retry_attempts: 3
  retry_backoff_seconds: [1, 2, 4]

monitoring:
  metrics_port: 9090
  alerts:
    discord_webhook_env: "DISCORD_WEBHOOK_URL"
    telegram_bot_token_env: "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: "TELEGRAM_CHAT_ID"
  daily_summary_hour: 18
