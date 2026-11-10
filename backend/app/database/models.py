from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MONEY = Numeric(36, 18)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TradingConfig(Base):
    __tablename__ = "trading_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    indicator: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    position_size_type: Mapped[str] = mapped_column(String(16), default="fixed")  # fixed | percent
    position_size_value: Mapped[Decimal] = mapped_column(MONEY)
    execution_mode: Mapped[str] = mapped_column(String(8), default="PAPER")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AppState(Base):
    """Single-row table holding persisted controls and risk limits."""

    __tablename__ = "app_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    emergency_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    max_position_size: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    max_daily_loss: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    max_open_positions: Mapped[int] = mapped_column(Integer, default=1)
    min_balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("config_id", "candle_ts", "signal_type", name="uq_signal_per_candle"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    config_id: Mapped[int | None] = mapped_column(ForeignKey("trading_configs.id"), nullable=True)
    indicator: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    signal_type: Mapped[str] = mapped_column(String(8))
    signal_price: Mapped[Decimal] = mapped_column(MONEY)
    candle_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    mode: Mapped[str] = mapped_column(String(8))
    execution_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    mode: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16), default="market")
    requested_quantity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    requested_quote_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    limit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    filled_quantity: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    average_fill_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    fee_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    config_id: Mapped[int | None] = mapped_column(ForeignKey("trading_configs.id"), nullable=True)
    mode: Mapped[str] = mapped_column(String(8), index=True)
    indicator: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8), default="LONG")
    entry_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    exit_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    entry_price: Mapped[Decimal] = mapped_column(MONEY)
    exit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    quantity: Mapped[Decimal] = mapped_column(MONEY)
    open_quantity: Mapped[Decimal] = mapped_column(MONEY)
    entry_cost: Mapped[Decimal] = mapped_column(MONEY)
    exit_proceeds: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    entry_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_signal_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_signal_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(8), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PaperBalance(Base):
    __tablename__ = "paper_balances"

    currency: Mapped[str] = mapped_column(String(16), primary_key=True)
    available: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
