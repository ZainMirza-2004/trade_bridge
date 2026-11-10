from enum import StrEnum


class ExecutionMode(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class SignalType(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SignalStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(StrEnum):
    CREATED = "created"  # persisted locally, not yet sent
    UNKNOWN = "unknown"  # submission outcome uncertain (timeout) — must be reconciled
    PENDING_NEW = "pending_new"
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    REPLACED = "replaced"
    FAILED = "failed"  # never reached the exchange / definitively not placed

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_ORDER_STATUSES


TERMINAL_ORDER_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.REPLACED,
    OrderStatus.FAILED,
}


class TradeStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ConnectionStatus(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    AUTH_FAILED = "AUTH_FAILED"
    API_UNAVAILABLE = "API_UNAVAILABLE"
