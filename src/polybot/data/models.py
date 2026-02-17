"""Core data models for the Polymarket trading bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Direction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    FOK = "FOK"  # Fill or Kill
    GTC = "GTC"  # Good Till Cancelled


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class AlertLevel(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class PriceLevel:
    price: float
    size: float


@dataclass
class Market:
    id: str
    question: str
    slug: str
    outcomes: list[str]
    token_ids: list[str]
    end_date: datetime
    category: str
    active: bool
    volume_24h: float = 0.0
    liquidity: float = 0.0


@dataclass
class OrderBook:
    market_id: str
    timestamp: datetime
    bids: list[PriceLevel]
    asks: list[PriceLevel]

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 1.0

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def bid_depth(self) -> float:
        return sum(level.size for level in self.bids)

    @property
    def ask_depth(self) -> float:
        return sum(level.size for level in self.asks)

    @property
    def book_imbalance(self) -> float:
        total = self.bid_depth + self.ask_depth
        if total == 0:
            return 0.0
        return (self.bid_depth - self.ask_depth) / total


@dataclass
class Trade:
    market_id: str
    timestamp: datetime
    side: Side
    price: float
    size: float
    outcome: str


@dataclass
class MarketSnapshot:
    market: Market
    orderbook: OrderBook
    recent_trades: list[Trade]
    vwap_1h: float = 0.0
    vwap_24h: float = 0.0
    volume_profile: dict = field(default_factory=dict)
    volatility_1h: float = 0.0
    price_history: list[float] = field(default_factory=list)


@dataclass
class Signal:
    market_id: str
    strategy: str
    direction: Direction
    outcome: str
    target_price: float
    confidence: float
    size_pct: float
    reason: str
    metadata: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    ttl: timedelta = field(default_factory=lambda: timedelta(minutes=5))

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.timestamp + self.ttl


@dataclass
class Order:
    market_id: str
    token_id: str
    side: Side
    price: float
    size: float
    order_type: OrderType
    strategy: str
    signal_id: str = ""
    order_id: str = ""
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    expires_at: datetime | None = None
    filled_size: float = 0.0
    avg_fill_price: float = 0.0


@dataclass
class Fill:
    order_id: str
    market_id: str
    token_id: str
    side: Side
    price: float
    size: float
    timestamp: datetime


@dataclass
class Position:
    market_id: str
    token_id: str
    outcome: str
    side: Side
    size: float
    avg_entry_price: float
    current_price: float = 0.0
    opened_at: datetime = field(default_factory=datetime.utcnow)
    strategy: str = ""

    @property
    def notional(self) -> float:
        return self.size * self.avg_entry_price

    @property
    def unrealized_pnl(self) -> float:
        if self.side == Side.BUY:
            return self.size * (self.current_price - self.avg_entry_price)
        return self.size * (self.avg_entry_price - self.current_price)


@dataclass
class Portfolio:
    positions: list[Position] = field(default_factory=list)
    realized_pnl: float = 0.0
    daily_pnl: float = 0.0
    balance: float = 0.0

    @property
    def total_exposure(self) -> float:
        return sum(p.notional for p in self.positions)

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions)

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl


class RiskDecision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REDUCE = "REDUCE"


@dataclass
class RiskCheckResult:
    decision: RiskDecision
    reason: str = ""
    modified_size: float | None = None


@dataclass
class ExitSignal:
    position: Position
    reason: str
    urgency: str = "normal"  # "normal" or "immediate"
