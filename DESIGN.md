# Polymarket Trading Bot — System Design Document

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Core Components](#core-components)
4. [Data Pipeline](#data-pipeline)
5. [Strategy Engine](#strategy-engine)
6. [Risk Management](#risk-management)
7. [Order Execution](#order-execution)
8. [Monitoring & Observability](#monitoring--observability)
9. [Configuration](#configuration)
10. [Project Structure](#project-structure)
11. [Technology Stack](#technology-stack)
12. [Deployment](#deployment)
13. [Security Considerations](#security-considerations)
14. [Development Roadmap](#development-roadmap)

---

## 1. Overview

### Purpose

A modular, event-driven trading bot for [Polymarket](https://polymarket.com) — a decentralized prediction market built on Polygon. The bot automates market discovery, signal generation, order placement, and position management for binary outcome markets.

### Goals

- **Automated trading**: Discover and trade prediction markets based on configurable strategies
- **Risk-managed**: Enforce position limits, drawdown controls, and portfolio-level constraints
- **Extensible**: Plugin-based strategy system allowing custom signal logic
- **Observable**: Full logging, metrics, and alerting for live operation
- **Reliable**: Graceful error handling, reconnection logic, and state persistence

### Non-Goals

- This bot is NOT a market maker (no continuous two-sided quoting by default)
- This bot does NOT provide financial advice
- This bot does NOT circumvent Polymarket terms of service

---

## 2. Architecture

### High-Level Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        Polymarket Bot                           │
│                                                                 │
│  ┌──────────┐   ┌──────────────┐   ┌────────────────────────┐  │
│  │  Market   │──▶│   Strategy   │──▶│   Order Execution      │  │
│  │  Scanner  │   │   Engine     │   │   Engine               │  │
│  └──────────┘   └──────────────┘   └────────────────────────┘  │
│       │               │                      │                  │
│       ▼               ▼                      ▼                  │
│  ┌──────────┐   ┌──────────────┐   ┌────────────────────────┐  │
│  │  Data     │   │   Risk       │   │   Position             │  │
│  │  Pipeline │   │   Manager    │   │   Manager              │  │
│  └──────────┘   └──────────────┘   └────────────────────────┘  │
│       │               │                      │                  │
│       └───────────────┴──────────────────────┘                  │
│                        │                                        │
│                 ┌──────▼──────┐                                  │
│                 │  State Store │                                  │
│                 │  (SQLite)    │                                  │
│                 └─────────────┘                                  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Monitoring / Alerting Layer                  │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
                ┌───────────────────────┐
                │  Polymarket CLOB API  │
                │  (REST + WebSocket)   │
                └───────────────────────┘
```

### Design Principles

1. **Event-driven**: Components communicate via an internal async event bus
2. **Separation of concerns**: Each module has a single responsibility
3. **Fail-safe defaults**: Bot pauses trading on unhandled errors rather than placing bad orders
4. **Idempotent operations**: Order placement and state updates are idempotent where possible
5. **Dry-run first**: Every strategy can be backtested and paper-traded before live execution

---

## 3. Core Components

### 3.1 Market Scanner

Discovers and filters Polymarket markets worth trading.

```python
class MarketScanner:
    """
    Periodically fetches active markets from the Polymarket API
    and filters them based on configurable criteria.
    """

    async def scan(self) -> list[Market]:
        # Fetch active markets from Polymarket CLOB API
        # Filter by: volume, liquidity, time-to-resolution, category
        # Return ranked list of tradeable markets
        ...

    async def subscribe(self, market_id: str):
        # Subscribe to real-time orderbook updates via WebSocket
        ...
```

**Filtering criteria:**
- Minimum 24h volume threshold (e.g., > $10,000)
- Minimum orderbook depth (e.g., > $5,000 on each side)
- Time to resolution window (e.g., resolves in 1–30 days)
- Category allowlist/blocklist (e.g., politics, crypto, sports)
- Spread threshold (e.g., bid-ask spread < 5%)

### 3.2 Data Pipeline

Collects, normalizes, and stores market data for strategy consumption.

```python
class DataPipeline:
    """
    Ingests data from multiple sources, normalizes it,
    and provides a unified interface for strategies.
    """

    async def ingest_orderbook(self, market_id: str, data: OrderBook):
        # Store orderbook snapshot, compute derived metrics
        ...

    async def ingest_trades(self, market_id: str, trades: list[Trade]):
        # Store trade history, update VWAP, volume profiles
        ...

    async def get_market_snapshot(self, market_id: str) -> MarketSnapshot:
        # Return current state: price, volume, book depth, signals
        ...
```

**Data sources:**
- Polymarket CLOB REST API — market metadata, historical prices
- Polymarket CLOB WebSocket — real-time orderbook and trade updates
- External data feeds — news APIs, social sentiment (optional, strategy-dependent)

### 3.3 Strategy Engine

Pluggable system for signal generation and trade decisions.

```python
class BaseStrategy(ABC):
    """Abstract base class for all trading strategies."""

    @abstractmethod
    async def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        """Evaluate market data and return a trade signal or None."""
        ...

    @abstractmethod
    def get_params(self) -> StrategyParams:
        """Return current strategy parameters for logging."""
        ...
```

**Built-in strategies (see Section 5 for details):**
- `MeanReversionStrategy` — Trade toward fair value when price deviates
- `MomentumStrategy` — Follow strong directional moves
- `ArbitrageStrategy` — Exploit mispricings between correlated markets
- `SentimentStrategy` — Trade based on external news/social signals

### 3.4 Risk Manager

Enforces risk limits before any order reaches the exchange.

```python
class RiskManager:
    """
    Pre-trade and portfolio-level risk checks.
    Every order must pass through the RiskManager before execution.
    """

    def check_order(self, order: Order, portfolio: Portfolio) -> RiskDecision:
        # Validate against all risk rules
        # Return APPROVE, REJECT, or REDUCE (with modified size)
        ...
```

**Risk rules (see Section 6 for details):**
- Maximum position size per market
- Maximum total portfolio exposure
- Maximum daily loss (drawdown circuit breaker)
- Maximum number of concurrent positions
- Minimum time between trades (anti-churn)

### 3.5 Order Execution Engine

Handles order lifecycle: creation, submission, monitoring, and fills.

```python
class ExecutionEngine:
    """
    Manages order placement and lifecycle on the Polymarket CLOB.
    """

    async def submit_order(self, order: Order) -> OrderResult:
        # Sign order with wallet, submit to CLOB API
        # Monitor for fill, partial fill, or rejection
        ...

    async def cancel_order(self, order_id: str) -> bool:
        # Cancel an open order
        ...

    async def get_open_orders(self) -> list[Order]:
        # Fetch current open orders
        ...
```

### 3.6 Position Manager

Tracks all open positions and their P&L in real time.

```python
class PositionManager:
    """
    Tracks positions, computes P&L, and triggers exits.
    """

    def update_position(self, fill: Fill):
        # Update position from a fill event
        ...

    def get_portfolio(self) -> Portfolio:
        # Return current portfolio state
        ...

    def check_exits(self) -> list[ExitSignal]:
        # Check stop-loss, take-profit, and time-based exits
        ...
```

---

## 4. Data Pipeline

### 4.1 Data Flow

```
Polymarket API ──▶ Raw Ingestion ──▶ Normalization ──▶ Storage ──▶ Strategy Access
                                                          │
                                                          ▼
                                                    Derived Metrics
                                                   (VWAP, volatility,
                                                    book imbalance)
```

### 4.2 Data Models

```python
@dataclass
class Market:
    id: str                     # Polymarket condition_id
    question: str               # Market question text
    slug: str                   # URL slug
    outcomes: list[str]         # e.g., ["Yes", "No"]
    token_ids: list[str]        # CLOB token IDs for each outcome
    end_date: datetime          # Resolution date
    category: str               # Market category
    active: bool                # Is market still trading
    volume_24h: float           # 24-hour volume in USDC
    liquidity: float            # Total orderbook liquidity

@dataclass
class OrderBook:
    market_id: str
    timestamp: datetime
    bids: list[PriceLevel]      # [(price, size), ...]
    asks: list[PriceLevel]      # [(price, size), ...]
    best_bid: float
    best_ask: float
    mid_price: float
    spread: float

@dataclass
class Trade:
    market_id: str
    timestamp: datetime
    side: Side                  # BUY or SELL
    price: float                # 0.00 to 1.00
    size: float                 # Size in USDC
    outcome: str                # "Yes" or "No"

@dataclass
class MarketSnapshot:
    market: Market
    orderbook: OrderBook
    recent_trades: list[Trade]
    vwap_1h: float
    vwap_24h: float
    volume_profile: dict
    book_imbalance: float       # (bid_depth - ask_depth) / total_depth
    volatility_1h: float
    price_history: list[float]  # Recent price samples
```

### 4.3 Storage

- **SQLite** for persistent state (positions, order history, P&L)
- **In-memory ring buffers** for real-time orderbook and trade data
- **Optional**: TimescaleDB/InfluxDB for long-term analytics

### 4.4 WebSocket Management

```python
class WebSocketManager:
    """
    Manages WebSocket connections to Polymarket CLOB.
    Handles reconnection, heartbeats, and message routing.
    """

    async def connect(self):
        # Establish WebSocket connection with auto-reconnect
        ...

    async def subscribe_market(self, token_id: str):
        # Subscribe to orderbook and trade updates
        ...

    async def on_message(self, message: dict):
        # Route message to appropriate handler
        ...
```

**Reconnection policy:**
- Exponential backoff: 1s, 2s, 4s, 8s, 16s, max 60s
- Automatic resubscription to all active markets on reconnect
- Stale data detection: flag data older than 30s as stale

---

## 5. Strategy Engine

### 5.1 Signal Model

```python
@dataclass
class Signal:
    market_id: str
    strategy: str               # Strategy name that generated this signal
    direction: Direction        # BUY or SELL
    outcome: str                # "Yes" or "No"
    target_price: float         # Desired entry price (0.00–1.00)
    confidence: float           # 0.0–1.0, used for position sizing
    size_pct: float             # Suggested size as % of available capital
    reason: str                 # Human-readable explanation
    metadata: dict              # Strategy-specific data
    timestamp: datetime
    ttl: timedelta              # Signal expiry (e.g., 5 minutes)
```

### 5.2 Strategy: Mean Reversion

Trades when market price deviates significantly from estimated fair value.

```
Fair Value Estimation:
  - VWAP-based: Use 1h/24h VWAP as fair value anchor
  - Book-imbalance adjusted: Shift fair value based on order flow

Entry Signal:
  - |mid_price - fair_value| > threshold (e.g., 3%)
  - Direction: BUY if mid < fair_value, SELL if mid > fair_value
  - Confidence scales with deviation magnitude

Exit Signal:
  - Price reverts to within 1% of fair value
  - Stop-loss at 2x entry deviation
  - Time-based exit if no reversion in N minutes
```

### 5.3 Strategy: Momentum

Follows strong directional moves confirmed by volume.

```
Entry Signal:
  - Price change > threshold over lookback window (e.g., 5% in 1h)
  - Volume confirmation: current volume > 2x average
  - Book imbalance confirms direction

Exit Signal:
  - Momentum fades (price change reverses direction)
  - Take profit at target (e.g., +3% from entry)
  - Trailing stop-loss (e.g., 2% from peak)
```

### 5.4 Strategy: Arbitrage

Exploits mispricings between related markets.

```
Detection:
  - Complementary markets: Yes + No prices should sum to ~$1.00
  - Correlated markets: Related questions with divergent pricing
  - Cross-platform: Price differences vs. other prediction markets

Entry Signal:
  - Spread exceeds transaction costs + minimum profit threshold
  - Both legs have sufficient liquidity

Exit Signal:
  - Spread converges
  - One leg becomes illiquid
  - Time-based exit
```

### 5.5 Strategy: Sentiment

Trades based on external news and social media signals. (Optional/advanced.)

```
Data Sources:
  - News APIs (e.g., NewsAPI, GDELT)
  - Social media (e.g., Twitter/X API)
  - On-chain data (wallet flows, resolution oracle activity)

Signal Generation:
  - NLP-based sentiment scoring of relevant content
  - Sudden sentiment shift → trade in sentiment direction
  - Requires market-to-topic mapping
```

### 5.6 Strategy Composition

Multiple strategies can run simultaneously. The engine aggregates signals:

```python
class StrategyAggregator:
    """
    Combines signals from multiple strategies.
    Handles conflicts and produces final trade decisions.
    """

    def aggregate(self, signals: list[Signal]) -> list[Signal]:
        # Group by market
        # If signals agree: boost confidence
        # If signals conflict: use priority ranking or skip
        # Apply minimum confidence threshold
        ...
```

---

## 6. Risk Management

### 6.1 Pre-Trade Checks

Every order passes through these checks before submission:

| Rule                     | Default Limit       | Description                                      |
|--------------------------|---------------------|--------------------------------------------------|
| `max_position_size`      | $500 per market     | Maximum notional per single market                |
| `max_portfolio_exposure` | $5,000 total        | Maximum total notional across all markets         |
| `max_positions`          | 10 concurrent       | Maximum number of open positions                  |
| `max_daily_loss`         | $250 (5% of capital)| Daily drawdown circuit breaker                    |
| `min_trade_interval`     | 30 seconds          | Minimum time between trades on same market        |
| `max_order_size`         | $200 per order      | Maximum single order notional                     |
| `max_slippage`           | 2%                  | Maximum acceptable slippage from target price     |

### 6.2 Portfolio-Level Controls

```python
class PortfolioRiskMonitor:
    """
    Continuously monitors portfolio health.
    Can trigger emergency actions.
    """

    async def monitor(self):
        while True:
            portfolio = self.position_manager.get_portfolio()

            # Check daily P&L
            if portfolio.daily_pnl < -self.config.max_daily_loss:
                await self.emergency_stop("Daily loss limit breached")

            # Check total exposure
            if portfolio.total_exposure > self.config.max_portfolio_exposure:
                await self.reduce_exposure()

            # Check individual position health
            for position in portfolio.positions:
                if position.unrealized_pnl < -position.stop_loss:
                    await self.exit_position(position, reason="stop_loss")

            await asyncio.sleep(5)
```

### 6.3 Circuit Breakers

| Trigger                          | Action                              |
|----------------------------------|-------------------------------------|
| Daily loss > 5% of capital       | Halt all new trades for the day     |
| Single trade loss > 2% of capital| Pause strategy for 1 hour           |
| 3 consecutive losses             | Reduce position sizes by 50%        |
| API errors > 5 in 1 minute       | Pause trading, alert operator       |
| WebSocket disconnect > 2 minutes | Cancel all open orders              |

---

## 7. Order Execution

### 7.1 Order Types

Polymarket CLOB supports limit orders. The bot uses these order types:

```python
@dataclass
class Order:
    market_id: str
    token_id: str               # Specific outcome token
    side: Side                  # BUY or SELL
    price: float                # Limit price (0.01–0.99)
    size: float                 # Size in outcome tokens
    order_type: OrderType       # LIMIT, FOK (fill-or-kill), GTC
    strategy: str               # Which strategy generated this
    signal_id: str              # Link back to the signal
    created_at: datetime
    expires_at: datetime | None
```

### 7.2 Execution Flow

```
Signal ──▶ Risk Check ──▶ Order Construction ──▶ Wallet Signing ──▶ API Submission
                                                                         │
                                                                         ▼
                                                                   Order Monitoring
                                                                         │
                                                            ┌────────────┼────────────┐
                                                            ▼            ▼            ▼
                                                         Filled    Partial Fill   Rejected
                                                            │            │            │
                                                            ▼            ▼            ▼
                                                      Update Position  Retry/Cancel  Log & Alert
```

### 7.3 Polymarket CLOB Integration

The bot integrates with the Polymarket CLOB (Central Limit Order Book) API:

**Authentication:**
- Ethereum wallet (private key) for signing orders
- API key for REST endpoints
- EIP-712 typed data signing for order placement

**Key endpoints:**
- `GET /markets` — List active markets
- `GET /book` — Get orderbook for a token
- `GET /trades` — Get recent trades
- `POST /order` — Place a signed order
- `DELETE /order/{id}` — Cancel an order
- `GET /positions` — Get current positions
- WebSocket — Real-time orderbook and trade streams

```python
class PolymarketClient:
    """
    Wrapper around the Polymarket CLOB API.
    Handles authentication, request signing, and rate limiting.
    """

    def __init__(self, private_key: str, api_key: str):
        self.signer = EthWalletSigner(private_key)
        self.api_key = api_key
        self.rate_limiter = RateLimiter(max_requests=10, per_seconds=1)
        self.base_url = "https://clob.polymarket.com"

    async def place_order(self, order: Order) -> OrderResult:
        # Build order payload
        # Sign with EIP-712
        # Submit to CLOB API
        # Return result with order ID
        ...

    async def get_orderbook(self, token_id: str) -> OrderBook:
        ...

    async def get_markets(self, **filters) -> list[Market]:
        ...
```

### 7.4 Rate Limiting

- Polymarket CLOB rate limits: ~10 requests/second
- Bot enforces stricter internal limits: 5 requests/second with burst allowance
- Token bucket algorithm for smooth rate limiting

---

## 8. Monitoring & Observability

### 8.1 Logging

Structured JSON logging with context:

```python
logger.info("order_placed", extra={
    "market_id": order.market_id,
    "side": order.side,
    "price": order.price,
    "size": order.size,
    "strategy": order.strategy,
})
```

**Log levels:**
- `DEBUG` — Orderbook updates, signal evaluations
- `INFO` — Orders placed/filled, position changes, strategy decisions
- `WARNING` — Partial fills, rate limit approaching, stale data
- `ERROR` — API failures, order rejections, risk limit breaches
- `CRITICAL` — Circuit breaker triggered, emergency stop

### 8.2 Metrics

Key metrics tracked (exportable to Prometheus/Grafana):

| Metric                      | Type      | Description                          |
|-----------------------------|-----------|--------------------------------------|
| `bot_pnl_total`             | Gauge     | Total realized + unrealized P&L      |
| `bot_pnl_daily`             | Gauge     | Today's P&L                          |
| `bot_positions_open`        | Gauge     | Number of open positions             |
| `bot_exposure_total`        | Gauge     | Total portfolio notional             |
| `bot_orders_placed`         | Counter   | Orders placed (by strategy)          |
| `bot_orders_filled`         | Counter   | Orders filled                        |
| `bot_orders_rejected`       | Counter   | Orders rejected                      |
| `bot_signals_generated`     | Counter   | Signals generated (by strategy)      |
| `bot_api_latency`           | Histogram | API request latency                  |
| `bot_ws_reconnects`         | Counter   | WebSocket reconnection count         |
| `bot_risk_rejections`       | Counter   | Orders rejected by risk manager      |

### 8.3 Alerting

Notifications via configurable channels (Discord webhook, Telegram, email):

- **Trade alerts**: Every order placed and filled
- **P&L alerts**: Daily summary, large single-trade P&L
- **Risk alerts**: Circuit breaker triggers, drawdown warnings
- **System alerts**: API errors, WebSocket disconnects, high latency

```python
class AlertManager:
    async def send_alert(self, level: AlertLevel, message: str, data: dict):
        for channel in self.channels:
            await channel.send(level, message, data)
```

---

## 9. Configuration

### 9.1 Configuration File

All parameters are configurable via YAML:

```yaml
# config.yaml

bot:
  name: "polymarket-bot"
  mode: "paper"  # "paper" or "live"
  log_level: "INFO"
  data_dir: "./data"

wallet:
  # Private key loaded from environment variable
  private_key_env: "POLYMARKET_PRIVATE_KEY"
  api_key_env: "POLYMARKET_API_KEY"

scanner:
  interval_seconds: 300
  min_volume_24h: 10000
  min_liquidity: 5000
  max_spread_pct: 5.0
  categories_allowlist: []        # Empty = all categories
  categories_blocklist: []
  resolution_window_days: [1, 30] # Min and max days to resolution

strategies:
  enabled:
    - name: "mean_reversion"
      weight: 1.0
      params:
        deviation_threshold: 0.03
        lookback_minutes: 60
        exit_threshold: 0.01
        stop_loss_multiplier: 2.0
        max_hold_minutes: 120

    - name: "momentum"
      weight: 0.5
      params:
        price_change_threshold: 0.05
        lookback_minutes: 60
        volume_multiplier: 2.0
        take_profit: 0.03
        trailing_stop: 0.02

  aggregation:
    min_confidence: 0.5
    conflict_resolution: "skip"  # "skip", "highest_confidence", "priority"

risk:
  max_position_size: 500
  max_portfolio_exposure: 5000
  max_positions: 10
  max_daily_loss: 250
  min_trade_interval_seconds: 30
  max_order_size: 200
  max_slippage_pct: 2.0
  circuit_breakers:
    consecutive_losses_pause: 3
    consecutive_losses_size_reduction: 0.5
    api_errors_per_minute_pause: 5
    ws_disconnect_cancel_seconds: 120

execution:
  rate_limit_per_second: 5
  order_ttl_seconds: 300
  retry_attempts: 3
  retry_backoff_seconds: [1, 2, 4]

monitoring:
  metrics_port: 9090
  alerts:
    discord_webhook_env: "DISCORD_WEBHOOK_URL"
    telegram_bot_token_env: "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: "TELEGRAM_CHAT_ID"
  daily_summary_hour: 18  # UTC
```

### 9.2 Environment Variables

Secrets are never stored in config files:

```
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_API_KEY=...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

---

## 10. Project Structure

```
BTC-Bot/
├── DESIGN.md                   # This document
├── README.md                   # Quick start guide
├── pyproject.toml              # Project metadata and dependencies
├── config.yaml                 # Default configuration
├── config.example.yaml         # Example config (committed to repo)
├── .env.example                # Example environment variables
│
├── src/
│   └── polybot/
│       ├── __init__.py
│       ├── main.py             # Entry point, orchestrator
│       ├── config.py           # Configuration loading and validation
│       ├── events.py           # Event bus implementation
│       │
│       ├── data/
│       │   ├── __init__.py
│       │   ├── client.py       # Polymarket CLOB API client
│       │   ├── websocket.py    # WebSocket connection manager
│       │   ├── pipeline.py     # Data ingestion and normalization
│       │   ├── models.py       # Data models (Market, OrderBook, Trade, etc.)
│       │   └── storage.py      # SQLite persistence layer
│       │
│       ├── scanner/
│       │   ├── __init__.py
│       │   └── scanner.py      # Market discovery and filtering
│       │
│       ├── strategies/
│       │   ├── __init__.py
│       │   ├── base.py         # BaseStrategy ABC and Signal model
│       │   ├── mean_reversion.py
│       │   ├── momentum.py
│       │   ├── arbitrage.py
│       │   ├── sentiment.py
│       │   └── aggregator.py   # Multi-strategy signal aggregation
│       │
│       ├── risk/
│       │   ├── __init__.py
│       │   ├── manager.py      # Pre-trade risk checks
│       │   ├── portfolio.py    # Portfolio-level monitoring
│       │   └── circuit_breaker.py
│       │
│       ├── execution/
│       │   ├── __init__.py
│       │   ├── engine.py       # Order submission and lifecycle
│       │   ├── signer.py       # EIP-712 order signing
│       │   └── rate_limiter.py # Token bucket rate limiter
│       │
│       ├── positions/
│       │   ├── __init__.py
│       │   └── manager.py      # Position tracking and P&L
│       │
│       └── monitoring/
│           ├── __init__.py
│           ├── metrics.py      # Prometheus metrics
│           ├── alerts.py       # Alert channels (Discord, Telegram)
│           └── dashboard.py    # Optional web dashboard
│
├── tests/
│   ├── conftest.py
│   ├── test_client.py
│   ├── test_scanner.py
│   ├── test_strategies.py
│   ├── test_risk.py
│   ├── test_execution.py
│   └── test_positions.py
│
├── scripts/
│   ├── backtest.py             # Historical backtesting runner
│   └── paper_trade.py          # Paper trading mode launcher
│
└── docker/
    ├── Dockerfile
    └── docker-compose.yml
```

---

## 11. Technology Stack

| Component       | Technology                        | Rationale                                   |
|-----------------|-----------------------------------|---------------------------------------------|
| Language        | Python 3.12+                      | Async support, rich ecosystem, rapid dev    |
| Async runtime   | asyncio + aiohttp                 | Native async for I/O-bound trading bot      |
| WebSocket       | aiohttp / websockets              | Real-time market data streaming             |
| Blockchain      | web3.py / eth-account             | Wallet signing, EIP-712 typed data          |
| Database        | SQLite (aiosqlite)                | Zero-config, embedded, sufficient for state |
| Config          | PyYAML + pydantic                 | Typed, validated configuration              |
| Logging         | structlog                         | Structured JSON logging                     |
| Metrics         | prometheus-client                 | Industry-standard metrics export            |
| HTTP client     | aiohttp / httpx                   | Async HTTP for API calls                    |
| Testing         | pytest + pytest-asyncio           | Standard Python testing                     |
| Packaging       | uv / pip                          | Fast dependency management                  |
| Containerization| Docker                            | Reproducible deployment                     |

---

## 12. Deployment

### 12.1 Local Development

```bash
# Clone and setup
git clone <repo-url>
cd BTC-Bot
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Configure
cp config.example.yaml config.yaml
cp .env.example .env
# Edit .env with your credentials

# Paper trade
python -m polybot --mode paper

# Live trade (use with caution)
python -m polybot --mode live
```

### 12.2 Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY src/ src/
COPY config.yaml .
CMD ["python", "-m", "polybot"]
```

```yaml
# docker-compose.yml
services:
  bot:
    build: .
    env_file: .env
    volumes:
      - ./data:/app/data
      - ./config.yaml:/app/config.yaml
    restart: unless-stopped
```

### 12.3 Production Considerations

- Run on a VPS close to Polymarket infrastructure (low latency)
- Use systemd or Docker with restart policies for uptime
- Rotate logs with logrotate or Docker log drivers
- Back up SQLite database regularly
- Use separate wallets for testing and production
- Monitor wallet balance and gas (MATIC for Polygon transactions)

---

## 13. Security Considerations

### Secrets Management
- Private keys loaded from environment variables only, never hardcoded
- `.env` file excluded from version control via `.gitignore`
- Consider using a secrets manager (AWS Secrets Manager, Vault) in production

### Wallet Security
- Use a dedicated wallet with limited funds for the bot
- Never store the main wallet private key on the bot server
- Set up wallet balance alerts

### API Security
- Validate all data from Polymarket API (don't trust external input)
- Use HTTPS for all API communication
- Implement request signing verification for WebSocket messages

### Operational Security
- Run bot as a non-root user
- Use read-only filesystem where possible
- Network firewall: only allow outbound to Polymarket APIs
- Audit logging for all state-changing operations

---

## 14. Development Roadmap

### Phase 1: Foundation (Weeks 1–2)
- [ ] Project setup (repo, CI, linting, testing framework)
- [ ] Polymarket CLOB API client (REST + WebSocket)
- [ ] Data models and storage layer
- [ ] Market scanner with basic filtering
- [ ] Configuration system

### Phase 2: Core Trading (Weeks 3–4)
- [ ] Strategy engine with mean reversion strategy
- [ ] Risk manager with basic pre-trade checks
- [ ] Order execution engine with wallet signing
- [ ] Position manager with P&L tracking
- [ ] Paper trading mode

### Phase 3: Risk & Reliability (Weeks 5–6)
- [ ] Circuit breakers and portfolio-level risk
- [ ] WebSocket reconnection and stale data handling
- [ ] Comprehensive error handling and recovery
- [ ] State persistence and restart recovery
- [ ] Backtesting framework

### Phase 4: Monitoring & Strategies (Weeks 7–8)
- [ ] Structured logging and metrics export
- [ ] Alert system (Discord/Telegram)
- [ ] Momentum strategy
- [ ] Arbitrage strategy
- [ ] Strategy aggregation

### Phase 5: Production Hardening (Weeks 9–10)
- [ ] Docker deployment
- [ ] Performance optimization
- [ ] Security audit
- [ ] Documentation
- [ ] Live testing with small capital

---

## Appendix A: Polymarket CLOB API Reference

The Polymarket CLOB (Central Limit Order Book) is the primary trading venue.

**Base URL:** `https://clob.polymarket.com`

**Key concepts:**
- **Condition ID**: Unique identifier for a market (question)
- **Token ID**: Unique identifier for an outcome token (Yes or No)
- **Prices**: Range from 0.01 to 0.99 (representing probability)
- **Settlement**: Markets resolve to 0 (No) or 1 (Yes); winning tokens are redeemable for $1 USDC

**Order signing**: Orders are signed using EIP-712 typed data with the user's Ethereum private key. The CLOB verifies the signature before accepting the order.

## Appendix B: Glossary

| Term            | Definition                                                    |
|-----------------|---------------------------------------------------------------|
| CLOB            | Central Limit Order Book — Polymarket's trading engine        |
| USDC            | USD Coin — stablecoin used for trading on Polymarket          |
| Polygon         | Layer-2 blockchain where Polymarket operates                  |
| EIP-712         | Ethereum standard for typed structured data signing           |
| VWAP            | Volume-Weighted Average Price                                 |
| Book imbalance  | Ratio of bid-side depth to ask-side depth                     |
| Circuit breaker | Automatic trading halt triggered by adverse conditions        |
| Paper trading   | Simulated trading without real money                          |
| Slippage        | Difference between expected and actual execution price        |
