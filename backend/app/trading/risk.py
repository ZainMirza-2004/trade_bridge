"""Pre-trade risk checks. Pure functions over a snapshot so they are trivially testable."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.decimal_utils import ZERO


@dataclass
class RiskLimits:
    max_position_size: Decimal | None = None  # max quote value of a single entry
    max_daily_loss: Decimal | None = None  # positive number; entries blocked once today's realised loss reaches it
    max_open_positions: int = 1
    min_balance: Decimal = ZERO  # quote balance that must remain after an entry


@dataclass
class EntryDecision:
    allowed: bool
    amount: Decimal = ZERO
    reason: str | None = None


def size_entry(position_size_type: str, position_size_value: Decimal, available_quote: Decimal) -> Decimal:
    if position_size_type == "percent":
        return available_quote * position_size_value / Decimal(100)
    return position_size_value


def check_entry(
    limits: RiskLimits,
    requested_amount: Decimal,
    available_quote: Decimal,
    open_positions: int,
    realized_pnl_today: Decimal,
    min_order_quote: Decimal,
) -> EntryDecision:
    if open_positions >= limits.max_open_positions:
        return EntryDecision(False, reason=f"Max open positions ({limits.max_open_positions}) reached")
    if limits.max_daily_loss is not None and limits.max_daily_loss > 0 and -realized_pnl_today >= limits.max_daily_loss:
        return EntryDecision(False, reason=f"Max daily loss ({limits.max_daily_loss}) reached")
    amount = requested_amount
    if limits.max_position_size is not None and limits.max_position_size > 0 and amount > limits.max_position_size:
        amount = limits.max_position_size
    if amount <= 0:
        return EntryDecision(False, reason="Trade amount must be positive")
    if amount > available_quote:
        return EntryDecision(False, reason=f"Insufficient balance: need {amount}, available {available_quote}")
    if available_quote - amount < limits.min_balance:
        return EntryDecision(False, reason=f"Entry would breach minimum balance {limits.min_balance}")
    if amount < min_order_quote:
        return EntryDecision(False, reason=f"Amount {amount} below exchange minimum {min_order_quote}")
    return EntryDecision(True, amount=amount)
