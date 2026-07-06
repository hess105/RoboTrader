"""Domain models shared by every layer.

These are the ONLY types that cross layer boundaries: strategies emit Signals,
the risk manager turns Signals into OrderIntents, execution turns intents into
Orders/Fills, and the journal records all of them.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum


class Mode(str, Enum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(str, Enum):
    PENDING_SUBMIT = "pending_submit"   # persisted locally, not yet acked by broker
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class Bar:
    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class Signal:
    """What a strategy wants, with its reasoning. No sizes here — sizing is risk's job."""
    strategy: str
    symbol: str
    side: Side
    reason: str                 # human-readable, goes to the audit log verbatim
    stop_price: Decimal | None  # strategy-proposed protective stop
    ts: datetime
    limit_price: Decimal | None = None  # optional resting-limit entry; None = market


@dataclass(frozen=True)
class OrderIntent:
    """A risk-approved, sized order. client_order_id is generated ONCE here and
    reused across submit retries — this is the idempotency key (see execution/)."""
    client_order_id: str
    signal: Signal
    notional: Decimal | None    # fractional orders size by notional
    qty: Decimal | None
    limit_price: Decimal | None
    is_protective_exit: bool = False   # protective exits bypass entry halts, never risk checks


@dataclass
class Order:
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    side: Side
    status: OrderStatus
    notional: Decimal | None
    qty: Decimal | None
    filled_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    submitted_at: datetime | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class Fill:
    broker_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    ts: datetime


@dataclass
class Position:
    symbol: str
    qty: Decimal
    avg_entry: Decimal
    stop_price: Decimal | None
    opened_at: datetime
    bucket: str                 # correlation bucket from config
    strategy: str = ""          # owning strategy (composite routes exits by this)


def new_client_order_id(strategy: str, symbol: str) -> str:
    """Deterministic-prefix, unique suffix. Alpaca dedupes on this string."""
    return f"rt-{strategy}-{symbol}-{uuid.uuid4().hex[:12]}"
